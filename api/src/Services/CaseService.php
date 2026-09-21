<?php

declare(strict_types=1);

namespace App\Services;

use App\ApiException;
use App\Database;
use App\Repositories\CaseRepository;
use PDO;
use Throwable;

/**
 * Case queries plus the three officer commands:
 *   accept           accept a discrepancy                       (ACCEPT_OVERRIDE)
 *   correct          approve a corrected BL, re-compare         (AUTHORIZE_CORRECTION)
 *   requestDocument  ask for a missing / replacement document   (MISSING_DOCUMENT)
 *
 * Every command: checks the officer, locks the case, checks the case is in a state
 * that allows the command, then records the decision, updates state and writes the
 * audit trail in ONE transaction. Nothing is ever changed automatically.
 */
final class CaseService
{
    private const REASONS = ['discrepancy', 'missing_attachment', 'wrong_doc_type', 'unreadable', 'missing_value'];
    private const DOC_TARGETS = ['SI', 'BL', 'BOTH'];

    public function __construct(private PDO $db, private CaseRepository $repo)
    {
    }

    public static function make(): self
    {
        $db = Database::connection();
        return new self($db, new CaseRepository($db));
    }

    // =================================================================== reads
    public function listCases(array $query): array
    {
        $status = null;
        if (($query['status'] ?? '') !== '') {
            $status = array_map(fn ($s) => strtoupper(trim($s)), explode(',', (string) $query['status']));
            $bad = array_diff($status, CaseRepository::STATUSES);
            if ($bad) {
                throw new ApiException(400, 'invalid_status', 'Unknown status: ' . implode(', ', $bad));
            }
        }
        $reason = ($query['reason'] ?? '') !== '' ? (string) $query['reason'] : null;
        if ($reason !== null && !in_array($reason, self::REASONS, true)) {
            throw new ApiException(400, 'invalid_reason', 'Unknown reason: ' . $reason);
        }
        $limit = min(200, max(1, (int) ($query['limit'] ?? 50)));
        $offset = max(0, (int) ($query['offset'] ?? 0));
        $q = trim((string) ($query['q'] ?? ''));

        [$rows, $total] = $this->repo->listCases(
            ['status' => $status, 'reason' => $reason, 'q' => $q !== '' ? $q : null],
            $limit,
            $offset
        );
        return [
            'data' => $rows,
            'meta' => [
                'total' => $total, 'limit' => $limit, 'offset' => $offset,
                'status_counts' => $this->repo->statusCounts(),
            ],
        ];
    }

    public function getCase(string $idOrRef): array
    {
        $id = $this->caseId($idOrRef);
        $task = $this->repo->latestReviewTask($id);
        return [
            'case' => $this->repo->caseRow($id),
            'emails' => $this->repo->emails($id),
            'documents' => $this->repo->documents($id),
            'comparison' => $this->repo->latestComparison($id),
            'extracted' => (object) $this->repo->extractedFields($id),
            'activities' => $this->repo->activities($id),
            'review_task' => $task,
            'allowed_actions' => $this->allowedActions($task),
            'decisions' => $this->repo->decisions($id),
            'audit' => $this->repo->audit($id),
        ];
    }

    public function officers(): array
    {
        return ['data' => $this->repo->officers()];
    }

    /** Decisions the officer may take on the case right now. */
    private function allowedActions(?array $task): array
    {
        if (!$task || !in_array($task['status'], ['OPEN', 'ASSIGNED'], true)) {
            return [];
        }
        return array_values($task['context']['suggested_decisions'] ?? []);
    }

    // =============================================================== commands
    /** Accept a discrepancy: the case closes as "accepted with discrepancies". */
    public function accept(string $idOrRef, array $body): array
    {
        $officer = $this->officer($body);
        $notes = $this->notes($body);
        $caseId = $this->caseId($idOrRef);

        $this->tx(function () use ($caseId, $officer, $notes) {
            $case = $this->lockCase($caseId);
            $task = $this->openTask($caseId);
            if ($case['status'] !== 'AWAITING_REVIEW' || !$task || $task['reason'] !== 'discrepancy') {
                throw new ApiException(409, 'invalid_state', 'Only a case with an open discrepancy can be accepted.');
            }
            $defects = $task['context']['defect_fields'] ?? [];

            $decisionId = $this->insertDecision(
                $task['id'], $caseId, $officer['id'], 'ACCEPT_OVERRIDE', $notes, ['defect_fields' => $defects]
            );
            $this->resolveTask($task['id'], $officer['id']);
            $this->finishHumanReview($caseId);
            $this->db->prepare(
                "update cases set status = 'RESOLVED', resolution = 'ACCEPTED_WITH_DISCREPANCIES',
                        resolution_notes = ?, resolved_at = now() where id = ?"
            )->execute([$notes ?? 'Discrepancy accepted by the case officer.', $caseId]);

            $this->audit($caseId, $officer['id'], 'DISCREPANCY_ACCEPTED', 'human_decision', $decisionId,
                'AWAITING_REVIEW', 'RESOLVED', ['defect_fields' => $defects, 'officer' => $officer['full_name']]);
        });
        return $this->getCase($caseId);
    }

    /**
     * Approve a correction: the BL gets a new version (v1 -> v2) whose chosen fields take the SI's
     * values, the old version is kept, and the SI and the new BL are compared again.
     * Only officers with can_authorize_corrections may do this.
     */
    public function correct(string $idOrRef, array $body): array
    {
        $officer = $this->officer($body);
        if (!$officer['can_authorize_corrections']) {
            throw new ApiException(403, 'not_authorized',
                $officer['full_name'] . ' is not authorized to approve document corrections.');
        }
        $notes = $this->notes($body);
        $caseId = $this->caseId($idOrRef);
        $requested = $body['fields'] ?? null;

        $this->tx(function () use ($caseId, $officer, $notes, $requested) {
            $case = $this->lockCase($caseId);
            $task = $this->openTask($caseId);
            if ($case['status'] !== 'AWAITING_REVIEW' || !$task || $task['reason'] !== 'discrepancy') {
                throw new ApiException(409, 'invalid_state', 'Only a case with an open discrepancy can be corrected.');
            }
            $run = $this->repo->latestComparison($caseId);
            if (!$run) {
                throw new ApiException(409, 'no_comparison', 'This case has no comparison to correct.');
            }
            $defects = array_values(array_map(
                fn ($i) => $i['field_name'],
                array_filter($run['items'], fn ($i) => !$i['is_match'])
            ));

            // which fields to correct: those requested, or every discrepancy
            if ($requested === null) {
                $fields = $defects;
            } else {
                if (!is_array($requested) || !$requested) {
                    throw new ApiException(422, 'invalid_fields', '"fields" must be a non-empty list of field names.');
                }
                $unknown = array_diff($requested, CaseRepository::FIELD_ORDER);
                if ($unknown) {
                    throw new ApiException(422, 'invalid_fields', 'Unknown field: ' . implode(', ', $unknown));
                }
                $notDefects = array_diff($requested, $defects);
                if ($notDefects) {
                    throw new ApiException(422, 'not_a_discrepancy',
                        'These fields already match: ' . implode(', ', $notDefects));
                }
                $fields = array_values(array_unique($requested));
            }

            $si = $this->currentDocument($caseId, 'SI');
            $bl = $this->currentDocument($caseId, 'BL');
            if (!$si || !$bl) {
                throw new ApiException(409, 'documents_missing', 'The case needs a current SI and BL to correct.');
            }
            $siFields = $this->currentFields($si['id']);
            $blFields = $this->currentFields($bl['id']);
            $newVersion = (int) $bl['version'] + 1;

            // 1. new BL version; the old one stays as history
            $this->db->prepare('update documents set is_current = false where id = ?')->execute([$bl['id']]);
            $newName = preg_replace('/(\.[^.]+)?$/', '_v' . $newVersion . '$1', (string) $bl['file_name'], 1);
            $st = $this->db->prepare(
                "insert into documents (case_id, email_id, doc_type, type_confidence, version, supersedes_id,
                                        is_current, file_name, language, created_by)
                 values (?, ?, 'BL', ?, ?, ?, true, ?, ?, 'HUMAN') returning id"
            );
            $st->execute([$caseId, $bl['email_id'], $bl['type_confidence'], $newVersion, $bl['id'], $newName,
                $bl['language']]);
            $newDocId = (string) $st->fetchColumn();

            // 2. activities: the correction, then the re-comparison
            $correctionAct = $this->insertActivity($caseId, $newDocId, 'DOCUMENT_CORRECTION');
            $comparisonAct = $this->insertActivity($caseId, null, 'COMPARISON');

            // 3. the new version's fields: unchanged, except corrected fields take the SI's values
            $insField = $this->db->prepare(
                'insert into extracted_fields (document_id, activity_id, field_name, raw_value, normalized_value,
                        numeric_value, confidence, extraction_attempt, is_human_corrected, is_current)
                 values (?, ?, ?, ?, ?, ?, ?, 1, ?, true)'
            );
            foreach (CaseRepository::FIELD_ORDER as $name) {
                $corrected = in_array($name, $fields, true);
                $src = $corrected ? ($siFields[$name] ?? null) : ($blFields[$name] ?? null);
                if ($src === null) {
                    continue;
                }
                $insField->execute([$newDocId, $correctionAct, $name, $src['raw_value'], $src['normalized_value'],
                    $src['numeric_value'], $src['confidence'], $corrected ? 't' : 'f']);
            }

            // 4. re-compare: corrected fields now match, every other field keeps its previous result
            $remaining = array_values(array_diff($defects, $fields));
            $result = $remaining ? 'MISMATCH' : 'MATCH';
            $st = $this->db->prepare(
                'insert into comparison_runs (case_id, activity_id, si_document_id, bl_document_id, result, mismatch_count)
                 values (?, ?, ?, ?, ?, ?) returning id'
            );
            $st->execute([$caseId, $comparisonAct, $si['id'], $newDocId, $result, count($remaining)]);
            $newRunId = (string) $st->fetchColumn();
            $insItem = $this->db->prepare(
                'insert into comparison_items (run_id, field_name, si_raw, bl_raw, si_normalized, bl_normalized, is_match)
                 values (?, ?, ?, ?, ?, ?, ?)'
            );
            foreach ($run['items'] as $i) {
                $fixed = in_array($i['field_name'], $fields, true);
                $insItem->execute([
                    $newRunId, $i['field_name'], $i['si_raw'],
                    $fixed ? $i['si_raw'] : $i['bl_raw'],
                    $i['si_normalized'],
                    $fixed ? $i['si_normalized'] : $i['bl_normalized'],
                    ($fixed || $i['is_match']) ? 't' : 'f',
                ]);
            }

            // 5. record the decision and the audit trail
            $decisionId = $this->insertDecision(
                $task['id'], $caseId, $officer['id'], 'AUTHORIZE_CORRECTION', $notes,
                ['fields' => $fields, 'from_version' => (int) $bl['version'], 'to_version' => $newVersion,
                    'corrected_to' => 'SI values'],
                $newDocId
            );
            $this->audit($caseId, $officer['id'], 'CORRECTION_AUTHORIZED', 'human_decision', $decisionId,
                null, null, ['fields' => $fields, 'officer' => $officer['full_name']]);
            $this->audit($caseId, $officer['id'], 'DOCUMENT_VERSION_CREATED', 'document', $newDocId, null, null,
                ['file_name' => $newName, 'from_version' => (int) $bl['version'], 'to_version' => $newVersion]);
            $this->audit($caseId, null, 'COMPARISON_COMPLETED', 'comparison_run', $newRunId, null, null,
                ['result' => $result, 'defect_fields' => $remaining], 'SYSTEM');

            // 6. fully corrected -> resolved; otherwise the rest stays open for the officer
            if (!$remaining) {
                $this->resolveTask($task['id'], $officer['id']);
                $this->finishHumanReview($caseId);
                $this->db->prepare(
                    "update cases set status = 'RESOLVED', resolution = 'CORRECTED', resolution_notes = ?,
                            resolved_at = now() where id = ?"
                )->execute([$notes ?? 'BL corrected to match the SI.', $caseId]);
                $this->audit($caseId, null, 'CASE_STATE_CHANGED', 'case', $caseId, 'AWAITING_REVIEW', 'RESOLVED',
                    ['resolution' => 'CORRECTED'], 'SYSTEM');
            } else {
                $this->db->prepare(
                    "update review_tasks set context = jsonb_set(context, '{defect_fields}', ?::jsonb),
                            description = ?, status = 'ASSIGNED', assigned_to = ?,
                            assigned_at = coalesce(assigned_at, now()) where id = ?"
                )->execute([
                    json_encode($remaining),
                    'SI and draft BL still differ on: ' . implode(', ', $remaining) . '.',
                    $officer['id'], $task['id'],
                ]);
            }
        });
        return $this->getCase($caseId);
    }

    /** Ask for a missing or replacement document. The case waits (AWAITING_DOCUMENT) until it arrives. */
    public function requestDocument(string $idOrRef, array $body): array
    {
        $officer = $this->officer($body);
        $notes = $this->notes($body);
        $target = strtoupper(trim((string) ($body['document'] ?? 'BOTH')));
        if (!in_array($target, self::DOC_TARGETS, true)) {
            throw new ApiException(422, 'invalid_document', '"document" must be SI, BL or BOTH.');
        }
        $caseId = $this->caseId($idOrRef);

        $this->tx(function () use ($caseId, $officer, $notes, $target) {
            $case = $this->lockCase($caseId);
            $task = $this->openTask($caseId);
            $suggested = $task['context']['suggested_decisions'] ?? [];
            if (!$task || !in_array('MISSING_DOCUMENT', $suggested, true) || $case['status'] === 'RESOLVED') {
                throw new ApiException(409, 'invalid_state',
                    'A document can only be requested for a case that is waiting on a document or a value.');
            }
            $decisionId = $this->insertDecision(
                $task['id'], $caseId, $officer['id'], 'MISSING_DOCUMENT', $notes,
                ['requested' => $target, 'reason' => $task['reason']]
            );
            $this->db->prepare(
                "update review_tasks set status = 'ASSIGNED', assigned_to = ?, assigned_at = coalesce(assigned_at, now())
                 where id = ?"
            )->execute([$officer['id'], $task['id']]);
            $this->db->prepare(
                "update activities set state = 'BLOCKED', blocked_reason = ?
                 where case_id = ? and activity_type = 'HUMAN_REVIEW'"
            )->execute(['Waiting for ' . ($target === 'BOTH' ? 'the SI and BL' : 'the ' . $target)
                . ' (requested by ' . $officer['full_name'] . ')', $caseId]);
            $this->db->prepare("update cases set status = 'AWAITING_DOCUMENT' where id = ?")->execute([$caseId]);

            $this->audit($caseId, $officer['id'], 'DOCUMENT_REQUESTED', 'human_decision', $decisionId,
                $case['status'], 'AWAITING_DOCUMENT',
                ['requested' => $target, 'reason' => $task['reason'], 'officer' => $officer['full_name']]);
        });
        return $this->getCase($caseId);
    }

    // ============================================================== helpers
    private function caseId(string $idOrRef): string
    {
        return $this->repo->findCaseId($idOrRef)
            ?? throw new ApiException(404, 'case_not_found', "No case '{$idOrRef}'.");
    }

    private function officer(array $body): array
    {
        $id = trim((string) ($body['officer_id'] ?? ''));
        if ($id === '') {
            throw new ApiException(400, 'officer_required', '"officer_id" is required.');
        }
        return $this->repo->officer($id)
            ?? throw new ApiException(422, 'unknown_officer', 'No active case officer with that id.');
    }

    private function notes(array $body): ?string
    {
        $notes = trim((string) ($body['notes'] ?? ''));
        if (mb_strlen($notes) > 2000) {
            throw new ApiException(422, 'notes_too_long', '"notes" can be at most 2000 characters.');
        }
        return $notes === '' ? null : $notes;
    }

    private function tx(callable $fn): mixed
    {
        $this->db->beginTransaction();
        try {
            $result = $fn();
            $this->db->commit();
            return $result;
        } catch (Throwable $e) {
            if ($this->db->inTransaction()) {
                $this->db->rollBack();
            }
            throw $e;
        }
    }

    /** Lock the case row so two officers (or a double click) cannot act at the same time. */
    private function lockCase(string $caseId): array
    {
        $st = $this->db->prepare('select id, status::text as status from cases where id = ? for update');
        $st->execute([$caseId]);
        return $st->fetch();
    }

    private function openTask(string $caseId): ?array
    {
        $st = $this->db->prepare(
            "select id, reason, context, status::text as status from review_tasks
             where case_id = ? and status::text in ('OPEN', 'ASSIGNED') order by created_at desc, id desc limit 1"
        );
        $st->execute([$caseId]);
        $task = $st->fetch();
        if (!$task) {
            return null;
        }
        $task['context'] = json_decode((string) $task['context'], true) ?? [];
        return $task;
    }

    private function resolveTask(string $taskId, string $officerId): void
    {
        $this->db->prepare(
            "update review_tasks set status = 'RESOLVED', assigned_to = ?, assigned_at = coalesce(assigned_at, now()),
                    resolved_at = now() where id = ?"
        )->execute([$officerId, $taskId]);
    }

    private function finishHumanReview(string $caseId): void
    {
        $this->db->prepare(
            "update activities set state = 'COMPLETED', blocked_reason = null, completed_at = now()
             where case_id = ? and activity_type = 'HUMAN_REVIEW'"
        )->execute([$caseId]);
    }

    private function insertDecision(string $taskId, string $caseId, string $officerId, string $decision,
                                    ?string $notes, array $payload, ?string $documentId = null): string
    {
        $st = $this->db->prepare(
            'insert into human_decisions (review_task_id, case_id, officer_id, decision, notes, payload,
                    resulting_document_id) values (?, ?, ?, ?, ?, ?::jsonb, ?) returning id'
        );
        $st->execute([$taskId, $caseId, $officerId, $decision, $notes,
            json_encode($payload ?: new \stdClass()), $documentId]);
        return (string) $st->fetchColumn();
    }

    private function insertActivity(string $caseId, ?string $documentId, string $type): string
    {
        $st = $this->db->prepare(
            "insert into activities (case_id, document_id, activity_type, state, attempt_count, started_at, completed_at)
             values (?, ?, ?, 'COMPLETED', 1, now(), now()) returning id"
        );
        $st->execute([$caseId, $documentId, $type]);
        return (string) $st->fetchColumn();
    }

    private function currentDocument(string $caseId, string $type): ?array
    {
        $st = $this->db->prepare(
            'select id, email_id, version, file_name, type_confidence, language from documents
             where case_id = ? and doc_type::text = ? and is_current limit 1'
        );
        $st->execute([$caseId, $type]);
        return $st->fetch() ?: null;
    }

    /** @return array<string, array> the document's current extracted fields, keyed by field name */
    private function currentFields(string $documentId): array
    {
        $st = $this->db->prepare(
            'select field_name, raw_value, normalized_value, numeric_value, confidence from extracted_fields
             where document_id = ? and is_current'
        );
        $st->execute([$documentId]);
        $out = [];
        foreach ($st->fetchAll() as $row) {
            $out[$row['field_name']] = $row;
        }
        return $out;
    }

    private function audit(string $caseId, ?string $actorId, string $event, string $entityType, ?string $entityId,
                           ?string $from, ?string $to, array $details, string $actor = 'HUMAN'): void
    {
        $this->db->prepare(
            'insert into audit_events (case_id, actor, actor_id, event_type, entity_type, entity_id, from_state,
                    to_state, details) values (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb)'
        )->execute([$caseId, $actor, $actorId, $event, $entityType, $entityId, $from, $to,
            json_encode($details ?: new \stdClass())]);
    }
}