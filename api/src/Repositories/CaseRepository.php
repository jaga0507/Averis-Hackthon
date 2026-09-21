<?php

declare(strict_types=1);

namespace App\Repositories;

use PDO;

/**
 * All the SQL that READS cases and the records around them.
 * Commands that change data live in CaseService, inside transactions.
 */
final class CaseRepository
{
    public const STATUSES = [
        'OPEN',
        'AWAITING_DOCUMENT',
        'AWAITING_REVIEW',
        'RESOLVED',
        'CLOSED',
    ];

    public const FIELD_ORDER = [
        'shipper',
        'consignee',
        'notify_party',
        'port_of_loading',
        'port_of_discharge',
        'container_count',
        'gross_weight_kg',
    ];

    public function __construct(private PDO $db)
    {
    }

    // ------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------

    private function all(
        string $sql,
        array $params = []
    ): array {
        $st = $this->db->prepare($sql);
        $st->execute($params);

        return $st->fetchAll(PDO::FETCH_ASSOC);
    }

    private function one(
        string $sql,
        array $params = []
    ): ?array {
        $rows = $this->all($sql, $params);

        return $rows[0] ?? null;
    }

    private function fieldOrderSql(
        string $column = 'field_name'
    ): string {
        return "array_position(
            array['"
            . implode("','", self::FIELD_ORDER)
            . "']::text[],
            {$column}
        )";
    }

    private static function decode(?string $json): array
    {
        if ($json === null || $json === '') {
            return [];
        }

        $decoded = json_decode($json, true);

        return is_array($decoded) ? $decoded : [];
    }

    // ------------------------------------------------------------
    // Cases
    // ------------------------------------------------------------

    /**
     * @return array{0: array, 1: int}
     */
    public function listCases(
        array $filters,
        int $limit,
        int $offset
    ): array {
        $where = [];
        $params = [];

        if (!empty($filters['status'])) {
            $where[] =
                'c.status::text in ('
                . implode(
                    ',',
                    array_fill(
                        0,
                        count($filters['status']),
                        '?'
                    )
                )
                . ')';

            array_push(
                $params,
                ...$filters['status']
            );
        }

        if (!empty($filters['reason'])) {
            $where[] = 'rt.reason = ?';
            $params[] = $filters['reason'];
        }

        if (!empty($filters['q'])) {
            $where[] = "
                (
                    c.case_ref ilike ?
                    or coalesce(c.title, '') ilike ?
                    or coalesce(c.shipment_ref, '') ilike ?
                )
            ";

            array_push(
                $params,
                '%' . $filters['q'] . '%',
                '%' . $filters['q'] . '%',
                '%' . $filters['q'] . '%'
            );
        }

        $whereSql = $where
            ? 'where ' . implode(' and ', $where)
            : '';

        $from = "
            from cases c

            left join lateral (
                select sender, category
                from emails
                where case_id = c.id
                order by created_at, id
                limit 1
            ) e on true

            left join lateral (
                select reason, status, context
                from review_tasks
                where case_id = c.id
                order by created_at desc, id desc
                limit 1
            ) rt on true
        ";

        $totalRow = $this->one(
            "
            select count(*) as n
            {$from}
            {$whereSql}
            ",
            $params
        );

        $total = (int) ($totalRow['n'] ?? 0);

        $rows = $this->all(
            "
            select
                c.id,
                c.case_ref,
                c.title,
                c.status::text as status,
                c.resolution::text as resolution,
                c.shipment_ref,
                c.created_at,
                e.sender,
                e.category::text as category,
                rt.reason as review_reason,
                rt.status::text as review_status,
                rt.context as review_context,

                (
                    select count(*)
                    from documents d
                    where d.case_id = c.id
                ) as document_count

            {$from}

            {$whereSql}

            order by c.case_ref
            limit ?
            offset ?
            ",
            array_merge(
                $params,
                [$limit, $offset]
            )
        );

        foreach ($rows as &$row) {
            $context = self::decode(
                $row['review_context'] ?? null
            );

            $row['defect_fields'] =
                $context['defect_fields'] ?? [];

            $row['document_count'] =
                (int) $row['document_count'];

            unset($row['review_context']);
        }

        unset($row);

        return [$rows, $total];
    }

    /**
     * @return array<string,int>
     */
    public function statusCounts(): array
    {
        $counts = array_fill_keys(
            self::STATUSES,
            0
        );

        foreach (
            $this->all(
                '
                select
                    status::text as status,
                    count(*) as n
                from cases
                group by 1
                '
            ) as $row
        ) {
            if (array_key_exists(
                $row['status'],
                $counts
            )) {
                $counts[$row['status']] =
                    (int) $row['n'];
            }
        }

        return $counts;
    }

    // ------------------------------------------------------------
    // One case
    // ------------------------------------------------------------

    public function findCaseId(
        string $idOrRef
    ): ?string {
        if (
            preg_match(
                '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
                $idOrRef
            )
        ) {
            $row = $this->one(
                'select id from cases where id = ?',
                [$idOrRef]
            );
        } else {
            $row = $this->one(
                '
                select id
                from cases
                where upper(case_ref) = upper(?)
                ',
                [$idOrRef]
            );
        }

        return $row['id'] ?? null;
    }

    public function caseRow(
        string $id
    ): ?array {
        return $this->one(
            '
            select
                id,
                case_ref,
                shipment_ref,
                title,
                status::text as status,
                resolution::text as resolution,
                resolution_notes,
                resolved_at,
                closed_at,
                created_at,
                updated_at
            from cases
            where id = ?
            ',
            [$id]
        );
    }

    // ------------------------------------------------------------
    // Emails
    // ------------------------------------------------------------

    public function emails(
        string $caseId
    ): array {
        return $this->all(
            '
            select
                id,
                external_id,
                sender,
                subject,
                body_text,
                received_at,
                category::text as category,
                classification_confidence,
                case_relationship::text as relationship
            from emails
            where case_id = ?
            order by created_at, id
            ',
            [$caseId]
        );
    }

    // ------------------------------------------------------------
    // Documents
    // ------------------------------------------------------------

    public function documents(
        string $caseId
    ): array {
        return $this->all(
            "
            select
                id,
                email_id,
                doc_type::text as doc_type,
                type_confidence,
                version,
                supersedes_id,
                is_current,
                file_name,
                mime_type,
                storage_path,
                created_by::text as created_by,
                created_at
            from documents
            where case_id = ?

            order by
                case doc_type::text
                    when 'SI' then 1
                    when 'BL' then 2
                    else 3
                end,
                version
            ",
            [$caseId]
        );
    }

    public function documentById(
        string $documentId
    ): ?array {
        return $this->one(
            '
            select
                id,
                case_id,
                email_id,
                doc_type::text as doc_type,
                version,
                is_current,
                file_name,
                mime_type,
                storage_path
            from documents
            where id = ?
            ',
            [$documentId]
        );
    }

    public function documentForEmailAndType(
        string $emailId,
        string $docType
    ): ?array {
        return $this->one(
            '
            select
                id,
                case_id,
                email_id,
                doc_type::text as doc_type,
                version,
                is_current,
                file_name,
                mime_type,
                storage_path
            from documents
            where email_id = ?
              and doc_type::text = ?
              and is_current = true
            order by version desc
            limit 1
            ',
            [
                $emailId,
                $docType,
            ]
        );
    }

    // ------------------------------------------------------------
    // Comparisons
    // ------------------------------------------------------------

    /**
     * Latest comparison run with its field-by-field items.
     */
    public function latestComparison(
        string $caseId
    ): ?array {
        $run = $this->one(
            '
            select
                id,
                si_document_id,
                bl_document_id,
                result::text as result,
                mismatch_count,
                created_at
            from comparison_runs
            where case_id = ?
            order by created_at desc, id desc
            limit 1
            ',
            [$caseId]
        );

        if (!$run) {
            return null;
        }

        $run['items'] = $this->all(
            '
            select
                field_name,
                si_raw,
                bl_raw,
                si_normalized,
                bl_normalized,
                is_match
            from comparison_items
            where run_id = ?
            order by '
            . $this->fieldOrderSql(),
            [$run['id']]
        );

        return $run;
    }

    // ------------------------------------------------------------
    // Extracted fields
    // ------------------------------------------------------------

    /**
     * Current extracted fields grouped by document id.
     */
    public function extractedFields(
        string $caseId
    ): array {
        $rows = $this->all(
            '
            select
                document_id,
                field_name,
                raw_value,
                normalized_value,
                numeric_value,
                confidence,
                is_human_corrected
            from extracted_fields
            where is_current
              and document_id in (
                  select id
                  from documents
                  where case_id = ?
              )
            order by
                document_id,
                '
            . $this->fieldOrderSql(),
            [$caseId]
        );

        $grouped = [];

        foreach ($rows as $row) {
            $documentId = $row['document_id'];

            unset($row['document_id']);

            $row['numeric_value'] =
                $row['numeric_value'] === null
                    ? null
                    : (float) $row['numeric_value'];

            $row['confidence'] =
                $row['confidence'] === null
                    ? null
                    : (float) $row['confidence'];

            $grouped[$documentId][] = $row;
        }

        return $grouped;
    }

    // ------------------------------------------------------------
    // Activities
    // ------------------------------------------------------------

    public function activities(
        string $caseId
    ): array {
        return $this->all(
            '
            select
                id,
                document_id,
                activity_type::text as activity_type,
                state::text as state,
                attempt_count,
                blocked_reason,
                last_error,
                started_at,
                completed_at
            from activities
            where case_id = ?
            order by created_at, activity_type
            ',
            [$caseId]
        );
    }

    // ------------------------------------------------------------
    // Review tasks
    // ------------------------------------------------------------

    public function latestReviewTask(
        string $caseId
    ): ?array {
        $task = $this->one(
            '
            select
                id,
                activity_id,
                reason,
                description,
                context,
                status::text as status,
                assigned_to,
                created_at,
                assigned_at,
                resolved_at
            from review_tasks
            where case_id = ?
            order by created_at desc, id desc
            limit 1
            ',
            [$caseId]
        );

        if ($task) {
            $task['context'] = self::decode(
                $task['context'] ?? null
            );
        }

        return $task;
    }

    // ------------------------------------------------------------
    // Human decisions
    // ------------------------------------------------------------

    public function decisions(
        string $caseId
    ): array {
        $rows = $this->all(
            '
            select
                hd.id,
                hd.decision::text as decision,
                hd.notes,
                hd.payload,
                hd.resulting_document_id,
                hd.created_at,
                o.id as officer_id,
                o.full_name as officer_name
            from human_decisions hd
            join case_officers o
                on o.id = hd.officer_id
            where hd.case_id = ?
            order by hd.created_at, hd.id
            ',
            [$caseId]
        );

        foreach ($rows as &$row) {
            $row['payload'] = self::decode(
                $row['payload'] ?? null
            );
        }

        unset($row);

        return $rows;
    }

    // ------------------------------------------------------------
    // Audit
    // ------------------------------------------------------------

    public function audit(
        string $caseId
    ): array {
        $rows = $this->all(
            '
            select
                id,
                actor::text as actor,
                actor_id,
                event_type,
                entity_type,
                entity_id,
                from_state,
                to_state,
                details,
                created_at
            from audit_events
            where case_id = ?
            order by created_at, id
            ',
            [$caseId]
        );

        foreach ($rows as &$row) {
            $row['details'] = self::decode(
                $row['details'] ?? null
            );
        }

        unset($row);

        return $rows;
    }

    // ------------------------------------------------------------
    // Officers
    // ------------------------------------------------------------

    public function officers(): array
    {
        return $this->all(
            '
            select
                id,
                full_name,
                email,
                can_authorize_corrections
            from case_officers
            where is_active
            order by full_name
            '
        );
    }

    public function officer(
        string $id
    ): ?array {
        if (
            !preg_match(
                '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
                $id
            )
        ) {
            return null;
        }

        return $this->one(
            '
            select
                id,
                full_name,
                email,
                can_authorize_corrections
            from case_officers
            where id = ?
              and is_active
            ',
            [$id]
        );
    }
}