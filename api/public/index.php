<?php

declare(strict_types=1);

use App\Database;
use Dotenv\Dotenv;
use Psr\Http\Message\ResponseInterface as Response;
use Psr\Http\Message\ServerRequestInterface as Request;
use Psr\Http\Server\RequestHandlerInterface as RequestHandler;
use Slim\Factory\AppFactory;

require __DIR__ . '/../vendor/autoload.php';

// .env lives in the project root (two levels above api/public)
Dotenv::createImmutable(dirname(__DIR__, 2))->load();

$app = AppFactory::create();

// --- Middleware ---------------------------------------------------------
// Slim runs the LAST-added middleware first, so CORS (added last) is the
// outermost layer and its headers are also attached to error responses.
$app->addBodyParsingMiddleware();
$app->addRoutingMiddleware();

$debug = filter_var($_ENV['APP_DEBUG'] ?? false, FILTER_VALIDATE_BOOLEAN);
$errorMiddleware = $app->addErrorMiddleware($debug, true, true);
$errorMiddleware->getDefaultErrorHandler()->forceContentType('application/json');   // API errors as JSON, not HTML

$app->add(function (Request $request, RequestHandler $handler): Response {
    // Answer browser preflight requests immediately
    $response = $request->getMethod() === 'OPTIONS'
        ? new Slim\Psr7\Response(204)
        : $handler->handle($request);

    return $response
        ->withHeader('Access-Control-Allow-Origin', $_ENV['CORS_ORIGIN'] ?? '*')
        ->withHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With, X-Officer-Id')
        ->withHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS');
});

// --- Helpers ------------------------------------------------------------
function json(Response $response, array $data, int $status = 200): Response
{
    $response->getBody()->write(json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
    return $response->withHeader('Content-Type', 'application/json')->withStatus($status);
}

// --- Routes -------------------------------------------------------------
(require __DIR__ . '/../src/routes.php')($app);

$app->run();