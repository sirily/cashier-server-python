"""
Cashier Server - Ledger-cli REST server for Cashier PWA
FastAPI implementation
"""

import base64
import os
import subprocess
from pathlib import Path
from typing import Optional
import uvicorn
from loguru import logger
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv


load_dotenv()
BEAN_FILE = os.getenv("BEANCOUNT_FILE")
CASHIER_SSL_KEY = os.getenv("CASHIER_SSL_KEY")
CASHIER_SSL_CERT = os.getenv("CASHIER_SSL_CERT")
CASHIER_ENABLE_SHUTDOWN = os.getenv("CASHIER_ENABLE_SHUTDOWN", "false").lower() in {"1", "true", "yes", "on"}
CASHIER_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CASHIER_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]

# Create a FastAPI instance
app = FastAPI(
    title="Cashier Server",
    description="Ledger-cli REST server for Cashier PWA",
    version="0.13.0",
)

# Configure CORS. In production the app is expected to be served same-origin
# through a reverse proxy, but keeping this configurable preserves local-dev
# compatibility with the original server. Credentialed CORS cannot be used with
# a wildcard origin, so credentials are only enabled for explicit origin lists.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CASHIER_CORS_ORIGINS if CASHIER_CORS_ORIGINS else ["*"],
    allow_credentials="*" not in CASHIER_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def index(query: Optional[str] = None):
    """Index. Based on the settings, it uses Ledger or Beancount for data."""
    if not query:
        return {"error": "No query provided"}

    if BEAN_FILE:
        return await beancount(query)
    else:
        return await ledger(query)


# @app.get("/")
async def ledger(query: Optional[str] = None):
    """
    Execute a ledger command and return the result.

    Args:
        query: The ledger command to execute

    Returns:
        The result of the ledger command
    """
    logger.info(f"Ledger query: {query}")

    try:
        # Execute the ledger command
        process = subprocess.run(
            ["ledger"] + query.split(),
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )

        if process.returncode == 0:
            output = process.stdout
        else:
            output = process.stderr

        result = output.splitlines()
        return result
    except subprocess.CalledProcessError as e:
        logger.error(f"Error executing ledger command: {e}")
        return {"error": str(e), "stderr": e.stderr}


def preload_beancount_data():
    """
    Pre-load beancount data into memory.
    """
    import beanquery

    if not BEAN_FILE:
        raise ValueError("BEAN_FILE environment variable not set")

    logger.info(f"Loading Beancount file: {BEAN_FILE}")

    connection = beanquery.connect("beancount:" + BEAN_FILE)
    return connection


async def beancount(query: Optional[str] = None):
    """
    Execute a beancount query and return the result.
    Requires Beancount to be installed.

    Args:
        query: The beancount command to execute

    Returns:
        The result of the beancount command
    """
    import beancount
    # import beanquery

    if not BEAN_FILE:
        raise ValueError("BEAN_FILE environment variable not set")
    if not query:
        return {"error": "No query provided"}

    logger.info(f"Beancount query: {query}")

    # connection = beanquery.connect("beancount:" + BEAN_FILE)
    connection = app.state.connection
    cursor = connection.execute(query)
    result = cursor.fetchall()

    # convert Inventory objects (sets) into lists that are JSON-serializable.
    for i, row in enumerate(result):
        # Convert the row to a list
        row_list = list(row)
        # Iterate over the tuple values
        for j, value in enumerate(row_list):
            if isinstance(value, beancount.Inventory):
                # Turn the Inventory into a simple list
                value = list(value)
                row_list[j] = value
        # Convert the list back to a tuple and set it back into the result
        result[i] = tuple(row_list)

    return result


@app.get("/reload")
def reload_beancount_data():
    """
    Reload the Beancount data.
    """
    import beanquery

    logger.info("Reloading Beancount data")
    assert BEAN_FILE
    # Just recreate the connection.
    app.state.connection = beanquery.connect("beancount:" + BEAN_FILE)

@app.get("/hello")
async def hello_img():
    """
    Return a base64-encoded image.
    """
    # This is a placeholder - you would need to replace with your actual image
    with open("hello.png", "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")

    return encoded_string


@app.get("/ping")
async def ping():
    """
    Simple ping endpoint to check if the server is running.
    """
    return "pong"


@app.get("/health")
async def health():
    """Return a small health payload suitable for container health checks."""
    # This endpoint is intentionally lightweight: it reports whether startup
    # loaded Beancount data, but does not execute a query on every health probe.
    return {
        "ok": True,
        "beancount_file_configured": bool(BEAN_FILE),
        "beancount_loaded": hasattr(app.state, "connection"),
    }


def resolve_infrastructure_path(file_path: str) -> Path:
    """Resolve an infrastructure path under the Beancount file directory.

    Cashier PWA asks for files such as config.bean/accounts.bean relative to the
    Beancount book. Do not allow absolute paths or ../ traversal outside the
    ledger directory.
    """
    if not BEAN_FILE:
        raise HTTPException(status_code=500, detail="BEANCOUNT_FILE environment variable not set")

    requested_path = Path(file_path)
    if requested_path.is_absolute():
        raise HTTPException(status_code=403, detail="Absolute paths are not allowed")

    ledger_root = Path(BEAN_FILE).resolve().parent
    resolved_path = (ledger_root / requested_path).resolve()

    try:
        resolved_path.relative_to(ledger_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Path traversal is not allowed") from exc

    return resolved_path


@app.get("/infrastructure")
async def infrastructure_file(file_path: str):
    """
    Provides a Beancount infrastructure file.
    For use with RustLedger.
    
    Args:
        file_path: The file path relative to the Beancount directory (e.g., "config.bean", "accounts.bean", "commodities.bean")
    
    Returns:
        The content of the requested file
    """
    full_file_path = resolve_infrastructure_path(file_path)
    if not full_file_path.exists() or not full_file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    with full_file_path.open("r", encoding="utf-8") as f:
        content = f.read()

    return {"content": content}


@app.get("/shutdown")
async def shutdown():
    """
    Shutdown the server.
    """
    logger.info("Shutdown requested")

    if not CASHIER_ENABLE_SHUTDOWN:
        raise HTTPException(status_code=403, detail="Shutdown endpoint is disabled")

    if hasattr(app.state, "server"):
        app.state.server.should_exit = True
        return {"message": "Server shut down"}
    else:
        return {"message": "Server not running"}


def main():
    """
    Entry point for the executable script.
    """
    logger.info("Starting Cashier Server on 0.0.0.0:3000")

    if BEAN_FILE:
        # pre-load beancount data
        app.state.connection = preload_beancount_data()

    # Create a server instance that can be referenced
    # uvicorn.run(app, host="0.0.0.0", port=3000)
    ssl_kwargs = {}
    if CASHIER_SSL_KEY and CASHIER_SSL_CERT:
        ssl_kwargs = {"ssl_keyfile": CASHIER_SSL_KEY, "ssl_certfile": CASHIER_SSL_CERT}
        logger.info(f"SSL enabled: key={CASHIER_SSL_KEY}, cert={CASHIER_SSL_CERT}")
    config = uvicorn.Config(app, host="0.0.0.0", port=3000, **ssl_kwargs)
    server = uvicorn.Server(config)

    # Store the server instance in the app state
    app.state.server = server

    server.run()


if __name__ == "__main__":
    main()
