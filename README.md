# Gemini API Key Rotator Proxy Server

This project is a FastAPI-based local proxy server designed for the Google Gemini API to using in Roo Code (Cline, Cursor, Windsurf, LibreChat, Continue, opencode).
It provides Gemini API key rotation, which helps distribute the load and bypass rate limits. This is particularly useful if you have multiple Free Tier Gemini keys and want to combine their rate limits.

The server offers two modes of operation:
1.  `main.py`: A native proxy that directly forwards requests to the Gemini API, automatically detecting the authentication type (API key or OAuth token).
2.  `main-openai.py`: An OpenAI API-compatible proxy. It accepts requests in the OpenAI format and adapts them for the Gemini API, allowing you to use existing tools and libraries developed for OpenAI.

## Key Features

-   **API Key Rotation**: Automatically cycles through a list of keys for each request.
-   **Proxy Support**: Option to route all outgoing traffic through a specified HTTP/SOCKS4/SOCKS5 proxy (e.g. local sing-box or some stuff from public proxy lists).
-   **Backoff Mechanism**: If a key fails (e.g., due to rate limiting), it is temporarily disabled with an exponentially increasing delay.
-   **Dual-Mode Operation**: Native and OpenAI-compatible modes.
-   **Streaming Support**: Correctly handles and forwards streaming responses from the API.
-   **Admin Endpoints**: Allows viewing the status of keys and reloading them without restarting the server.

## Requirements

-   Python 3.7+
-   `fastapi`
-   `uvicorn`
-   `httpx`

## Installation

1.  Clone the repository:
    ```bash
    git clone https://github.com/jwadow/gemini-api-key-rotator-proxy-server.git
    cd gemini-api-key-rotator-proxy-server
    ```

2.  Install the dependencies:
    ```bash
    pip install fastapi uvicorn httpx
    ```

## Configuration

1.  Create a file named `api_keys.txt` in the project's root directory.
2.  Add your Google Gemini API keys to this file, one per line.

    ```
    AIzaSy...key1
    AIzaSy...key2
    AIzaSy...key3
    ```

3.  (Optional) Configure the settings in `main.py` or `main-openai.py`:
    -   `VPN_PROXY_URL`: The URL of your HTTP proxy (e.g., `"192.168.1.103:2080"` or leave empty).
    -   `ADMIN_TOKEN`: A token for accessing the admin endpoints. It is recommended to change the default value.

## Running the Server

You can run the server in one of two modes.

### Native Mode

This mode directly proxies requests to the Gemini API.

```bash
uvicorn main:APP --host 127.0.0.1 --port 8000
```

### OpenAI-Compatible Mode (it has bugs!)

This mode allows you to use clients compatible with the OpenAI API.

```bash
uvicorn main-openai:APP --host 127.0.0.1 --port 8000
```

## Example Usage

### With `openai` Python library
The `test-openai.py` file demonstrates how to use the proxy with the `openai` Python library.

### With Roo Code

**For `main.py` (Native Mode):**
- Create a new API provider: "Google Gemini".
- Enable "Use custom base URL" and set it to `http://127.0.0.1:8000`.

**For `main-openai.py` (OpenAI-Compatible Mode):**
- Create a new API provider: "OpenAI Compatible".
- Set the Base URL to `http://127.0.0.1:8000`.
- Use any value for the API key (e.g., `changeme_local_only`).
- Specify the desired model, for example `gemini-2.5-pro`.