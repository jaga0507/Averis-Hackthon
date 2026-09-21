<?php

declare(strict_types=1);

use App\Controllers\CaseController;
use App\Controllers\ProcessingController;
use App\Database;
use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;
use Slim\App;
use Slim\Routing\RouteCollectorProxy;

return function (App $app): void {

    $app->get('/health', function (Request $request, Response $response): Response {
        return json($response, ['status' => 'ok']);
    });

    $app->get('/health/db', function (Request $request, Response $response): Response {
        $pdo = Database::connection();

        $info = $pdo->query(
            'select current_database() as database, current_user as db_user'
        )->fetch();

        $tables = $pdo->query(
            "select count(*)
             from information_schema.tables
             where table_schema = 'public'
             and table_type = 'BASE TABLE'"
        )->fetchColumn();

        return json($response, [
            'status'        => 'ok',
            'database'      => $info['database'],
            'db_user'       => $info['db_user'],
            'public_tables' => (int) $tables,
        ]);
    });

    $app->post('/processing/email', [ProcessingController::class, 'processEmail']);
    $app->post('/processing/case/{id}', [ProcessingController::class, 'processCase']);
    $app->get('/processing/health', [ProcessingController::class, 'health']);

    $app->get('/officers', [CaseController::class, 'officers']);

    $app->group('/cases', function (RouteCollectorProxy $group) {
        $group->get('', [CaseController::class, 'list']);
        $group->get('/{id}', [CaseController::class, 'show']);
        $group->post('/{id}/accept', [CaseController::class, 'accept']);
        $group->post('/{id}/correct', [CaseController::class, 'correct']);
        $group->post('/{id}/request-document', [CaseController::class, 'requestDocument']);
    });
};
