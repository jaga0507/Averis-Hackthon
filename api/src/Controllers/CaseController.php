<?php

declare(strict_types=1);

namespace App\Controllers;

use App\ApiException;
use App\Services\CaseService;
use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;

final class CaseController
{
    private ?CaseService $service = null;

    private function service(): CaseService
    {
        return $this->service ??= CaseService::make();
    }

    private function respond(Response $response, callable $fn): Response
    {
        try {
            return \json($response, $fn());
        } catch (ApiException $e) {
            return \json($response, ['error' => ['code' => $e->errorCode, 'message' => $e->getMessage()]], $e->status);
        }
    }

    private static function body(Request $request): array
    {
        $body = $request->getParsedBody();
        $data = is_array($body) ? $body : [];
        if (!isset($data['officer_id']) && $request->getHeaderLine('X-Officer-Id') !== '') {
            $data['officer_id'] = $request->getHeaderLine('X-Officer-Id');
        }
        return $data;
    }

    public function list(Request $request, Response $response): Response
    {
        return $this->respond($response, fn () => $this->service()->listCases($request->getQueryParams()));
    }

    public function show(Request $request, Response $response, array $args): Response
    {
        return $this->respond($response, fn () => $this->service()->getCase($args['id']));
    }

    public function accept(Request $request, Response $response, array $args): Response
    {
        return $this->respond($response, fn () => $this->service()->accept($args['id'], self::body($request)));
    }

    public function correct(Request $request, Response $response, array $args): Response
    {
        return $this->respond($response, fn () => $this->service()->correct($args['id'], self::body($request)));
    }

    public function requestDocument(Request $request, Response $response, array $args): Response
    {
        return $this->respond($response, fn () => $this->service()->requestDocument($args['id'], self::body($request)));
    }

    public function officers(Request $request, Response $response): Response
    {
        return $this->respond($response, fn () => $this->service()->officers());
    }
}