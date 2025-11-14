import os
from aiohttp import web
from datetime import datetime
import logging
import asyncio
import os

_LOGGER = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

async def start_health_server():
    """Start a lightweight HTTP server for health checks.

    Returns the port on success, or None if the server could not be started
    (for example, because the port is already in use).
    """
    port = int(os.environ.get('PORT', '8080'))
    app = web.Application()

    async def handle_root(request):
        return web.Response(text='OK', content_type='text/plain')

    async def handle_health(request):
        # Basic status
        resp = {
            'status': 'ok',
            'time': datetime.utcnow().isoformat()
        }

        # Report whether pymongo is installed and its version
        try:
            import pymongo
            resp['pymongo_installed'] = True
            resp['pymongo_version'] = getattr(pymongo, '__version__', None)
        except Exception:
            resp['pymongo_installed'] = False
            resp['pymongo_version'] = None

        # If MONGO_URI is set, try a quick ping in an executor to avoid blocking
        mongo_uri = os.environ.get('MONGO_URI')
        resp['mongo_uri_present'] = bool(mongo_uri)
        if mongo_uri and resp['pymongo_installed']:
            loop = asyncio.get_event_loop()

            def _ping_mongo():
                try:
                    # Import lazily to avoid import-time pymongo dependency elsewhere
                    from db.mongo_client_wrapper import get_mongo_client
                    client = get_mongo_client(mongo_uri, connect_timeout_ms=2000)
                    # quick ping
                    client.admin.command('ping')
                    return {'ok': True}
                except Exception as e:
                    return {'ok': False, 'error': str(e)}

            try:
                ping_result = await loop.run_in_executor(None, _ping_mongo)
                resp['mongo_ping'] = ping_result
            except Exception as e:
                resp['mongo_ping'] = {'ok': False, 'error': str(e)}

        return web.json_response(resp)

    app.add_routes([
        web.get('/', handle_root),
        web.get('/health', handle_health)
    ])

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        try:
            await site.start()
        except OSError as e:
            # Port likely in use; log and return None so caller can continue
            logger.warning(f"Health server could not bind to 0.0.0.0:{port}: {e}")
            # Attempt to clean up runner
            try:
                await runner.cleanup()
            except Exception:
                pass
            return None
    except Exception as e:
        logger.exception(f"Failed to start health server: {e}")
        try:
            await runner.cleanup()
        except Exception:
            pass
        return None

    # Keep running until canceled; aiohttp site runs in background on the loop
    return port
