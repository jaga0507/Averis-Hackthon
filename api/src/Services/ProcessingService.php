<?php

declare(strict_types=1);

namespace App\Services;

use App\ApiException;
use App\Database;
use App\Repositories\CaseRepository;
use PDO;
use Throwable;

final class ProcessingService
{
    private string $sidecarUrl;
    private string $supabaseUrl;
    private string $supabaseServiceKey;
    private string $storageBucket;

    public function __construct()
    {
        $this->sidecarUrl = rtrim(
            $_ENV['SIDECAR_URL'] ?? 'http://127.0.0.1:8001',
            '/'
        );

        $this->supabaseUrl = rtrim(
            $_ENV['SUPABASE_URL'] ?? '',
            '/'
        );

        $this->supabaseServiceKey =
            $_ENV['SUPABASE_SERVICE_KEY'] ?? '';

        $this->storageBucket =
            $_ENV['SUPABASE_STORAGE_BUCKET'] ?? '';

        if (
            $this->supabaseUrl === '' ||
            $this->supabaseServiceKey === '' ||
            $this->storageBucket === ''
        ) {
            throw new ApiException(
                500,
                'SUPABASE_STORAGE_CONFIG_MISSING',
                'Supabase Storage configuration is missing.'
            );
        }
    }

    public static function make(): self
    {
        return new self();
    }

    public function processEmail(array $email): array
    {
        $this->validateEmail($email);

        $attachments = $email['attachments'] ?? [];

        if (!is_array($attachments)) {
            throw new ApiException(
                400,
                'INVALID_EMAIL',
                'attachments must be an array.'
            );
        }

        $resolvedAttachments = [];

        foreach ($attachments as $attachment) {
            if (!is_array($attachment)) {
                throw new ApiException(
                    400,
                    'INVALID_ATTACHMENT',
                    'Each attachment must be an object.'
                );
            }

            $storagePath = $attachment['storage_path'] ?? null;

            if (
                !is_string($storagePath) ||
                trim($storagePath) === ''
            ) {
                throw new ApiException(
                    400,
                    'INVALID_ATTACHMENT',
                    'storage_path is required for each attachment.'
                );
            }

            $contents = $this->downloadStorageObject($storagePath);

            $resolvedAttachments[] = [
                'file_name' => $attachment['file_name']
                    ?? basename($storagePath),

                'mime_type' => $attachment['mime_type']
                    ?? 'application/octet-stream',

                'storage_path' => $storagePath,

                'content_base64' => base64_encode($contents),
            ];
        }

        $payloadEmail = $email;
        $payloadEmail['attachments'] = $resolvedAttachments;

        $payload = json_encode(
            $payloadEmail,
            JSON_UNESCAPED_UNICODE |
            JSON_UNESCAPED_SLASHES |
            JSON_THROW_ON_ERROR
        );

        $url = $this->sidecarUrl . '/process-email';

        $ch = curl_init($url);

        if ($ch === false) {
            throw new ApiException(
                502,
                'SIDECAR_INIT_FAILED',
                'Unable to initialize the Python processing request.'
            );
        }

        try {
            curl_setopt_array($ch, [
                CURLOPT_POST => true,
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_HTTPHEADER => [
                    'Content-Type: application/json',
                    'Accept: application/json',
                ],
                CURLOPT_POSTFIELDS => $payload,
                CURLOPT_CONNECTTIMEOUT => 5,
                CURLOPT_TIMEOUT => 120,
            ]);

            $body = curl_exec($ch);

            if ($body === false) {
                throw new ApiException(
                    502,
                    'SIDECAR_UNAVAILABLE',
                    'Python processing service is unavailable: '
                    . curl_error($ch)
                );
            }

            $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);

            if (!is_string($body) || $body === '') {
                throw new ApiException(
                    502,
                    'SIDECAR_EMPTY_RESPONSE',
                    'Python processing service returned an empty response.'
                );
            }

            try {
                $data = json_decode(
                    $body,
                    true,
                    512,
                    JSON_THROW_ON_ERROR
                );
            } catch (\JsonException $e) {
                throw new ApiException(
                    502,
                    'SIDECAR_INVALID_RESPONSE',
                    'Python processing service returned invalid JSON.'
                );
            }

            if ($status < 200 || $status >= 300) {
                $message =
                    'Python processing service returned HTTP ' . $status;

                if (is_array($data)) {
                    if (
                        isset($data['error']) &&
                        is_string($data['error'])
                    ) {
                        $message .= ': ' . $data['error'];
                    } elseif (
                        isset($data['message']) &&
                        is_string($data['message'])
                    ) {
                        $message .= ': ' . $data['message'];
                    }
                }

                throw new ApiException(
                    502,
                    'SIDECAR_PROCESSING_FAILED',
                    $message
                );
            }

            if (!is_array($data)) {
                throw new ApiException(
                    502,
                    'SIDECAR_INVALID_RESPONSE',
                    'Python processing service returned an invalid response.'
                );
            }

            return $data;
        } catch (ApiException $e) {
            throw $e;
        } catch (Throwable $e) {
            throw new ApiException(
                502,
                'SIDECAR_REQUEST_FAILED',
                'Python processing request failed: '
                . $e->getMessage()
            );
        } finally {
            curl_close($ch);
        }
    }

    public function processCase(string $caseId): array
    {
        $repository = new CaseRepository(Database::connection());

        $case = $repository->caseRow($caseId);

        if ($case === null) {
            throw new ApiException(
                404,
                'CASE_NOT_FOUND',
                'Case not found.'
            );
        }

        $emails = $repository->emails($caseId);

        if ($emails === []) {
            throw new ApiException(
                400,
                'CASE_HAS_NO_EMAIL',
                'Case has no email.'
            );
        }

        $email = $emails[count($emails) - 1];

        $documents = $repository->documents($caseId);

        $documents = array_values(array_filter(
            $documents,
            static function (array $document): bool {
                return
                    (bool) ($document['is_current'] ?? false)
                    && in_array(
                        $document['doc_type'] ?? null,
                        ['SI', 'BL'],
                        true
                    );
            }
        ));

        if ($documents === []) {
            throw new ApiException(
                400,
                'CASE_HAS_NO_DOCUMENTS',
                'Case has no current SI or BL documents.'
            );
        }

        $emailPayload = [
            'email_id' => $email['external_id'],
            'from' => $email['sender'],
            'subject' => $email['subject'],
            'body' => $email['body_text'],
            'attachments' => array_map(
                static function (array $document): array {
                    return [
                        'file_name' => $document['file_name'],
                        'mime_type' => $document['mime_type'],
                        'storage_path' => $document['storage_path'],
                    ];
                },
                $documents
            ),
        ];

        $result = $this->processEmail($emailPayload);

        $result['case_id'] = $caseId;
        $result['case_ref'] = $case['case_ref'];

        return $result;
    }

    public function processAndPersistCase(string $caseId): array
    {
        $result = $this->processCase($caseId);

        $db = Database::connection();

        $db->beginTransaction();

        try {
            $this->persistProcessingResult(
                $db,
                $caseId,
                $result
            );

            $db->commit();
        } catch (Throwable $e) {
            if ($db->inTransaction()) {
                $db->rollBack();
            }

            throw $e;
        }

        return $result;
    }

    private function persistProcessingResult(
        PDO $db,
        string $caseId,
        array $result
    ): void {
        $documents = $result['documents'] ?? [];
        $comparison = $result['comparison'] ?? null;

        $documentIds = [];

        foreach ($documents as $document) {
            $fileName = $document['file'] ?? null;
            $docType = $document['doc_type'] ?? null;

            if (
                !is_string($fileName) ||
                !is_string($docType)
            ) {
                continue;
            }

            $st = $db->prepare(
                'select id
                 from documents
                 where case_id = ?
                   and doc_type::text = ?
                   and is_current = true
                   and file_name = ?
                 order by version desc
                 limit 1'
            );

            $st->execute([
                $caseId,
                $docType,
                $fileName,
            ]);

            $id = $st->fetchColumn();

            if ($id !== false) {
                $documentIds[$docType] = (string) $id;
            }
        }

        /*
         * If the comparison result exists, record the comparison run.
         */
        if (
            is_array($comparison) &&
            isset($comparison['result'])
        ) {
            $siDocumentId = $documentIds['SI'] ?? null;
            $blDocumentId = $documentIds['BL'] ?? null;

            if ($siDocumentId === null || $blDocumentId === null) {
                throw new ApiException(
                    500,
                    'PROCESSING_DOCUMENT_LINK_FAILED',
                    'Unable to link the Python result to the current SI and BL documents.'
                );
            }

            $activityId = $this->insertActivity(
                $db,
                $caseId,
                null,
                'COMPARISON'
            );

            $st = $db->prepare(
                'insert into comparison_runs
                    (case_id, activity_id, si_document_id, bl_document_id,
                     result, mismatch_count)
                 values (?, ?, ?, ?, ?, ?)
                 returning id'
            );

            $st->execute([
                $caseId,
                $activityId,
                $siDocumentId,
                $blDocumentId,
                $comparison['result'],
                (int) ($comparison['mismatch_count'] ?? 0),
            ]);

            $runId = (string) $st->fetchColumn();

            $this->insertComparisonItems(
                $db,
                $runId,
                $comparison['items'] ?? []
            );

            $this->persistExtractedFields(
                $db,
                $caseId,
                $documentIds,
                $documents,
                $activityId
            );

            if ($comparison['result'] === 'MATCH') {
                $this->resolveAutomaticMatch(
                    $db,
                    $caseId,
                    $runId
                );
            } else {
                $this->createDiscrepancyReview(
                    $db,
                    $caseId,
                    $runId,
                    $comparison
                );
            }

            return;
        }

        /*
         * No comparison was needed.
         * Persist extraction/activity information only.
         */
        $this->persistExtractedFields(
            $db,
            $caseId,
            $documentIds,
            $documents,
            null
        );
    }

    private function persistExtractedFields(
        PDO $db,
        string $caseId,
        array $documentIds,
        array $documents,
        ?string $activityId
    ): void {
        $fields = [
            'shipper',
            'consignee',
            'notify_party',
            'port_of_loading',
            'port_of_discharge',
            'container_count',
            'gross_weight_kg',
        ];

        foreach ($documents as $document) {
            $docType = $document['doc_type'] ?? null;

            if (
                !is_string($docType) ||
                !isset($documentIds[$docType])
            ) {
                continue;
            }

            $documentId = $documentIds[$docType];

            $docFields = $document['fields'] ?? [];

            if (!is_array($docFields)) {
                continue;
            }

            /*
             * Make the previous current extraction non-current.
             */
            $db->prepare(
                'update extracted_fields
                 set is_current = false
                 where document_id = ?
                   and is_current = true'
            )->execute([$documentId]);

            $activity = $activityId;

            if ($activity === null) {
                $activity = $this->insertActivity(
                    $db,
                    $caseId,
                    $documentId,
                    'EXTRACTION'
                );
            }

            $st = $db->prepare(
                'insert into extracted_fields
                    (document_id, activity_id, field_name, raw_value,
                     normalized_value, numeric_value, confidence,
                     extraction_attempt, is_human_corrected, is_current)
                 values (?, ?, ?, ?, ?, ?, ?, ?, false, true)'
            );

            foreach ($fields as $fieldName) {
                $field = $docFields[$fieldName] ?? null;

                if (!is_array($field)) {
                    continue;
                }

                $rawValue = $field['value'] ?? null;
                $normalizedValue = $field['value'] ?? null;
                $numericValue = null;

                if ($fieldName === 'gross_weight_kg') {
                    $numericValue = $this->extractWeight(
                        $field['value'] ?? null
                    );
                }

                if ($fieldName === 'container_count') {
                    $numericValue = $this->extractContainerCount(
                        $field['value'] ?? null
                    );
                }

                $confidence = isset($field['confidence'])
                    ? (float) $field['confidence']
                    : null;

                $attempt = isset($document['attempts'])
                    ? (int) $document['attempts']
                    : 1;

                $st->execute([
                    $documentId,
                    $activity,
                    $fieldName,
                    $rawValue,
                    $normalizedValue,
                    $numericValue,
                    $confidence,
                    $attempt,
                ]);
            }
        }
    }

    private function insertComparisonItems(
        PDO $db,
        string $runId,
        array $items
    ): void {
        $st = $db->prepare(
            'insert into comparison_items
                (run_id, field_name, si_raw, bl_raw,
                 si_normalized, bl_normalized, is_match)
             values (?, ?, ?, ?, ?, ?, ?)'
        );

        foreach ($items as $item) {
            if (!is_array($item)) {
                continue;
            }

            $fieldName =
                $item['field_name']
                ?? $item['field']
                ?? null;

            if (!is_string($fieldName)) {
                continue;
            }

            $st->execute([
                $runId,
                $fieldName,
                $item['si_raw'] ?? null,
                $item['bl_raw'] ?? null,
                $item['si_normalized'] ?? null,
                $item['bl_normalized'] ?? null,
                !empty($item['is_match']) ? 't' : 'f',
            ]);
        }
    }

    private function resolveAutomaticMatch(
        PDO $db,
        string $caseId,
        string $runId
    ): void {
        $db->prepare(
            "update cases
             set status = 'RESOLVED',
                 resolution = 'MATCHED',
                 resolution_notes = 'SI and BL matched automatically.',
                 resolved_at = now()
             where id = ?"
        )->execute([$caseId]);

        $this->auditProcessing(
            $db,
            $caseId,
            'COMPARISON_COMPLETED',
            'comparison_run',
            $runId,
            null,
            null,
            [
                'result' => 'MATCH',
                'automated' => true,
            ]
        );

        $this->auditProcessing(
            $db,
            $caseId,
            'CASE_STATE_CHANGED',
            'case',
            $caseId,
            null,
            'RESOLVED',
            [
                'resolution' => 'MATCHED',
                'automated' => true,
            ]
        );
    }

    private function createDiscrepancyReview(
        PDO $db,
        string $caseId,
        string $runId,
        array $comparison
    ): void {
        $defects = $comparison['defect_fields'] ?? [];

        $description =
            'SI and draft BL differ on: '
            . implode(', ', $defects)
            . '.';

        $context = [
            'defect_fields' => array_values($defects),
            'suggested_decisions' => [
                'ACCEPT_OVERRIDE',
                'AUTHORIZE_CORRECTION',
            ],
            'comparison_run_id' => $runId,
        ];

        $st = $db->prepare(
            "insert into review_tasks
                (case_id, reason, description, context, status)
             values (?, 'discrepancy', ?, ?::jsonb, 'OPEN')
             returning id"
        );

        $st->execute([
            $caseId,
            $description,
            json_encode($context),
        ]);

        $taskId = (string) $st->fetchColumn();

        $db->prepare(
            "update cases
             set status = 'AWAITING_REVIEW'
             where id = ?"
        )->execute([$caseId]);

        $this->insertActivity(
            $db,
            $caseId,
            null,
            'HUMAN_REVIEW'
        );

        $this->auditProcessing(
            $db,
            $caseId,
            'COMPARISON_COMPLETED',
            'comparison_run',
            $runId,
            null,
            null,
            [
                'result' => 'MISMATCH',
                'defect_fields' => $defects,
                'automated' => true,
            ]
        );

        $this->auditProcessing(
            $db,
            $caseId,
            'REVIEW_TASK_CREATED',
            'review_task',
            $taskId,
            null,
            'AWAITING_REVIEW',
            [
                'reason' => 'discrepancy',
                'defect_fields' => $defects,
                'automated' => true,
            ]
        );
    }

    private function insertActivity(
        PDO $db,
        string $caseId,
        ?string $documentId,
        string $type
    ): string {
        $st = $db->prepare(
            "insert into activities
                (case_id, document_id, activity_type, state,
                 attempt_count, started_at, completed_at)
             values (?, ?, ?, 'COMPLETED', 1, now(), now())
             returning id"
        );

        $st->execute([
            $caseId,
            $documentId,
            $type,
        ]);

        return (string) $st->fetchColumn();
    }

    private function auditProcessing(
        PDO $db,
        string $caseId,
        string $event,
        string $entityType,
        ?string $entityId,
        ?string $from,
        ?string $to,
        array $details
    ): void {
        $st = $db->prepare(
            'insert into audit_events
                (case_id, actor, actor_id, event_type, entity_type,
                 entity_id, from_state, to_state, details)
             values (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb)'
        );

        $st->execute([
            $caseId,
            'SYSTEM',
            null,
            $event,
            $entityType,
            $entityId,
            $from,
            $to,
            json_encode($details ?: new \stdClass()),
        ]);
    }

    private function extractWeight(mixed $value): ?float
    {
        if (!is_string($value) && !is_numeric($value)) {
            return null;
        }

        $text = str_replace(',', '', (string) $value);

        if (preg_match('/-?\d+(?:\.\d+)?/', $text, $m)) {
            return (float) $m[0];
        }

        return null;
    }

    private function extractContainerCount(mixed $value): ?float
    {
        if (!is_string($value) && !is_numeric($value)) {
            return null;
        }

        if (preg_match('/^\s*(\d+)/', (string) $value, $m)) {
            return (float) $m[1];
        }

        return null;
    }

    private function downloadStorageObject(string $storagePath): string
    {
        $storagePath = ltrim($storagePath, '/');

        $url =
            $this->supabaseUrl .
            '/storage/v1/object/' .
            rawurlencode($this->storageBucket) .
            '/' .
            str_replace(
                '%2F',
                '/',
                rawurlencode($storagePath)
            );

        $ch = curl_init($url);

        if ($ch === false) {
            throw new ApiException(
                502,
                'STORAGE_INIT_FAILED',
                'Unable to initialize Supabase Storage request.'
            );
        }

        try {
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_HTTPHEADER => [
                    'Authorization: Bearer ' . $this->supabaseServiceKey,
                    'apikey: ' . $this->supabaseServiceKey,
                ],
                CURLOPT_CONNECTTIMEOUT => 5,
                CURLOPT_TIMEOUT => 60,
            ]);

            $body = curl_exec($ch);

            if ($body === false) {
                throw new ApiException(
                    502,
                    'STORAGE_UNAVAILABLE',
                    'Unable to download attachment from Supabase Storage: '
                    . curl_error($ch)
                );
            }

            $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);

            if ($status < 200 || $status >= 300) {
                throw new ApiException(
                    502,
                    'STORAGE_DOWNLOAD_FAILED',
                    'Supabase Storage returned HTTP '
                    . $status
                    . ' for '
                    . $storagePath
                );
            }

            return $body;
        } finally {
            curl_close($ch);
        }
    }

    public function health(): array
    {
        $url = $this->sidecarUrl . '/health';

        $ch = curl_init($url);

        if ($ch === false) {
            throw new ApiException(
                502,
                'SIDECAR_INIT_FAILED',
                'Unable to initialize the Python health request.'
            );
        }

        try {
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_HTTPHEADER => [
                    'Accept: application/json',
                ],
                CURLOPT_CONNECTTIMEOUT => 5,
                CURLOPT_TIMEOUT => 10,
            ]);

            $body = curl_exec($ch);

            if ($body === false) {
                throw new ApiException(
                    502,
                    'SIDECAR_UNAVAILABLE',
                    'Python processing service is unavailable: '
                    . curl_error($ch)
                );
            }

            $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);

            if ($status < 200 || $status >= 300) {
                throw new ApiException(
                    502,
                    'SIDECAR_HEALTH_FAILED',
                    'Python processing service returned HTTP ' . $status
                );
            }

            try {
                $data = json_decode(
                    $body,
                    true,
                    512,
                    JSON_THROW_ON_ERROR
                );
            } catch (\JsonException $e) {
                throw new ApiException(
                    502,
                    'SIDECAR_INVALID_RESPONSE',
                    'Python processing service returned invalid JSON.'
                );
            }

            if (!is_array($data)) {
                throw new ApiException(
                    502,
                    'SIDECAR_INVALID_RESPONSE',
                    'Python processing service returned an invalid health response.'
                );
            }

            return $data;
        } finally {
            curl_close($ch);
        }
    }

    private function validateEmail(array $email): void
    {
        if (
            !isset($email['email_id']) ||
            !is_string($email['email_id']) ||
            trim($email['email_id']) === ''
        ) {
            throw new ApiException(
                400,
                'INVALID_EMAIL',
                'email_id is required.'
            );
        }

        foreach (['from', 'subject', 'body'] as $field) {
            if (
                isset($email[$field]) &&
                !is_string($email[$field])
            ) {
                throw new ApiException(
                    400,
                    'INVALID_EMAIL',
                    $field . ' must be a string.'
                );
            }
        }

        if (
            isset($email['attachments']) &&
            !is_array($email['attachments'])
        ) {
            throw new ApiException(
                400,
                'INVALID_EMAIL',
                'attachments must be an array.'
            );
        }
    }
}