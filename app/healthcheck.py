import asyncio
import json
import logging
import time

logger = logging.getLogger("HealthCheck")
START_TIME = time.time()

health_check_callback = None

def set_health_checker(callback):
    global health_check_callback
    health_check_callback = callback

async def handle_request(reader, writer):
    try:
        data = await reader.read(1024)
        request_text = data.decode('utf-8', errors='ignore')
        
        lines = request_text.split('\r\n')
        path = "/"
        if lines and len(lines[0].split()) >= 2:
            path = lines[0].split()[1]
            
        if path in ("/healthcheck", "/health", "/"):
            is_healthy = True
            details = {}
            
            if health_check_callback:
                try:
                    is_healthy, details = await health_check_callback()
                except Exception as e:
                    is_healthy = False
                    details = {"error": str(e)}

            status_data = {
                "status": "ok" if is_healthy else "unhealthy",
                "uptime_seconds": int(time.time() - START_TIME),
                "details": details
            }
            body = json.dumps(status_data).encode('utf-8')
            status_code = b"200 OK" if is_healthy else b"500 Internal Server Error"
            
            response = (
                b"HTTP/1.1 " + status_code + b"\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode('utf-8') + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n" + body
            )
        else:
            body = b'{"error": "Not Found"}'
            response = (
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode('utf-8') + b"\r\n"
                b"Connection: close\r\n"
                b"\r\n" + body
            )
            
        writer.write(response)
        await writer.drain()
    except Exception as e:
        logger.error(f"HealthCheck handler error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()

async def start_healthcheck_server(host="0.0.0.0", port=8080, health_checker=None):
    if health_checker:
        set_health_checker(health_checker)
    server = await asyncio.start_server(handle_request, host, port)
    logger.info(f"🚀 HealthCheck HTTP server running on http://{host}:{port}/healthcheck")
    return server
