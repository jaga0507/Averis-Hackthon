<?php

declare(strict_types=1);

namespace App;

use RuntimeException;

/** An error the client should see: HTTP status + a short machine-readable code + a message. */
final class ApiException extends RuntimeException
{
    public function __construct(
        public readonly int $status,
        public readonly string $errorCode,
        string $message
    ) {
        parent::__construct($message);
    }
}