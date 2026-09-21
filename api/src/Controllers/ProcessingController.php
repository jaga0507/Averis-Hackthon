<?php

declare(strict_types=1);

namespace App\Controllers;

use App\ApiException;
use App\Services\ProcessingService;
use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;

final class ProcessingController
{
    private ?ProcessingService $service = null;

    private function service(): ProcessingService
    {
        return $this->service ??= ProcessingService::make();
    }

    private function respond(Response $response, callable $fn): Response
    {
        try {
            return \json($response, $fn());
        } catch (ApiException $e) {
            return \json(
                $response,
                [
                    'error' => [
                        'code' => $e->errorCode,
                        'message' => $e->getMessage(),
                    ],
                ],
                $e->status
            );
        }
    }

    public function processEmail(Request $request, Response $response): Response
    {
        return $this->respond($response, function () use ($request) {
            $body = $request->getParsedBody();

            if (!is_array($body)) {
                throw new ApiException(400, 'INVALID_REQUEST', 'Request body must be a JSON object.');
            }

            return [
                'status' => 'ok',
                'result' => $this->service()->processEmail($body),
            ];
        });
    }

    public function processCase(Request $request, Response $response, array $args): Response
    {
        return $this->respond($response, function () use ($args) {
            $caseId = $args['id'] ?? '';

            if (!is_string($caseId) || trim($caseId) === '') {
                throw new ApiException(400, 'INVALID_CASE_ID', 'Case ID is required.');
            }

return [
    'status' => 'ok',
    'result' => $this->service()->processAndPersistCase($caseId),
];
        });
    }

    public function health(Request $request, Response $response): Response
    {
        return $this->respond($response, fn () => $this->service()->health());
    }
}
