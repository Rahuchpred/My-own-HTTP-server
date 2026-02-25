Below is a **detailed Product Requirements Document (PRD)** for Codex explaining the complete plan and architecture in plain English.

This is intentionally written in implementation-ready clarity so Codex (or any AI dev agent) can execute step by step without guessing design intent.

---

# 📘 Product Requirements Document

## Project: Custom HTTP Server From Scratch (Python)

---

# 1. Executive Summary

The goal of this project is to build a fully functional HTTP server **from absolute scratch** in Python, without relying on high-level web frameworks such as Flask, Django, FastAPI, or even Python’s built-in `http.server` module.

The server will:

* Use raw TCP sockets
* Manually parse HTTP requests
* Construct HTTP responses
* Handle multiple clients
* Serve static files
* Support simple routing
* Process basic POST requests
* Be modular, readable, and extendable

The purpose of this project is educational and architectural: to deeply understand how HTTP, sockets, concurrency, and request-response lifecycles work at a low level.

This is not meant to compete with production servers like:

* Nginx
* Apache HTTP Server
* Gunicorn

Instead, this is a minimal, clean, structured HTTP server implementation that demonstrates core network protocol mechanics and server architecture fundamentals.

---

# 2. Goals and Non-Goals

## 2.1 Goals

The system must:

1. Listen on a TCP port.
2. Accept incoming connections.
3. Receive raw HTTP requests.
4. Parse:

   * Method (GET, POST)
   * Path
   * HTTP version
   * Headers
   * Body
5. Route based on path + method.
6. Return correctly formatted HTTP responses.
7. Serve static files (HTML, CSS, JS, images).
8. Support simple concurrency (threaded model).
9. Be structured as modular components.
10. Be readable and documented.

---

## 2.2 Non-Goals (Version 1)

The system will NOT include:

* HTTPS (TLS)
* HTTP/2
* WebSockets
* Advanced middleware system
* Async event loop (this will be v2)
* Full WSGI compatibility
* Session management
* Authentication

We focus purely on HTTP/1.1 basics.

---

# 3. High-Level Architecture

The server will follow this lifecycle:

```
Start Server
    ↓
Bind Socket
    ↓
Listen for Connections
    ↓
Accept Connection
    ↓
Read Raw Request
    ↓
Parse HTTP
    ↓
Route Request
    ↓
Construct Response
    ↓
Send Response
    ↓
Close Connection
```

---

# 4. System Components

We divide the system into clean modules:

```
project_root/
│
├── server.py
├── socket_handler.py
├── request.py
├── response.py
├── router.py
├── handlers/
│   └── example_handlers.py
├── static/
├── config.py
└── utils.py
```

Each file has a single responsibility.

---

# 5. Core Concepts

Before Codex implementation, these must be understood:

### 5.1 TCP Layer

HTTP runs on top of TCP.

We will use Python’s low-level socket API.

The socket must:

* Use `AF_INET`
* Use `SOCK_STREAM`
* Bind to a host and port
* Listen for connections
* Accept connections in loop

---

### 5.2 HTTP Structure

### Example Request

```
GET /index.html HTTP/1.1
Host: localhost:8080
User-Agent: Chrome
Accept: text/html

```

### Example Response

```
HTTP/1.1 200 OK
Content-Type: text/html
Content-Length: 123

<html>...</html>
```

Our server must manually construct this.

---

# 6. Module Breakdown

---

# 6.1 server.py

### Responsibility

Main application entry point.

### Responsibilities:

* Initialize server socket
* Accept connections
* Spawn handler thread per client
* Handle graceful shutdown

### Design

We define:

```
class HTTPServer:
    def __init__(...)
    def start()
    def stop()
    def _handle_client()
```

### Behavior

* Infinite loop
* Each client handled in new thread
* Logs errors but does not crash server

---

# 6.2 socket_handler.py

### Responsibility

Low-level socket interaction.

### Functions:

* read_from_socket(client_socket)
* write_to_socket(client_socket, data)

### Behavior:

* Read in chunks (1024 bytes)
* Detect end of headers using `\r\n\r\n`
* If Content-Length present, read full body

---

# 6.3 request.py

### Responsibility

Represents an HTTP request object.

### HTTPRequest Class

Attributes:

* method
* path
* http_version
* headers (dictionary)
* body

### Parsing Strategy

1. Split raw request into:

   * Header section
   * Body
2. Parse first line into:

   * method
   * path
   * version
3. Parse headers into dictionary.
4. Body is raw bytes or decoded string.

Must support:

* GET
* POST

---

# 6.4 response.py

### Responsibility

Represents HTTP response.

### HTTPResponse Class

Attributes:

* status_code
* reason_phrase
* headers
* body

### Methods:

* to_bytes()

Must output correct HTTP formatting:

```
HTTP/1.1 {status} {reason}
Header1: value
Header2: value

BODY
```

Must automatically set:

* Content-Length
* Content-Type (if known)

---

# 6.5 router.py

### Responsibility

Route requests to handlers.

### Router Class

Internally stores:

```
{
    ("GET", "/"): handler_function,
    ("POST", "/submit"): handler_function
}
```

### Methods:

* add_route(method, path, handler)
* resolve(method, path)

If route not found:
→ return 404 response

---

# 6.6 handlers/

Contains user-defined functions.

Example handler:

```
def home(request):
    return HTTPResponse(
        status_code=200,
        body="Hello World",
        headers={"Content-Type": "text/plain"}
    )
```

Handlers always receive:

* HTTPRequest
  and must return:
* HTTPResponse

---

# 7. Detailed Request Lifecycle

When client connects:

1. Client socket passed to thread.
2. Raw bytes read.
3. HTTPRequest constructed.
4. Router resolves handler.
5. Handler returns HTTPResponse.
6. Response converted to bytes.
7. Sent to client.
8. Connection closed.

---

# 8. Static File Support

Server must detect:

If path starts with `/static/`
→ Serve file from `/static` directory.

Process:

1. Map URL to filesystem path.
2. Prevent directory traversal (`../` attack).
3. Check file exists.
4. Read file.
5. Determine MIME type.
6. Return file bytes.

Example MIME mapping:

```
.html → text/html
.css → text/css
.js → application/javascript
.png → image/png
.jpg → image/jpeg
```

---

# 9. Concurrency Model

## Version 1 Strategy: Thread Per Request

On each `accept()`:

```
threading.Thread(target=handle_client).start()
```

### Why threads?

* Simpler
* Clear concurrency model
* Easy for educational purposes

### Risks

* Not scalable to 10,000 clients
* GIL limitations
* Memory overhead

This is acceptable.

Future version:

* Use async / select / epoll

---

# 10. Error Handling

Server must handle:

* Malformed requests
* Unsupported HTTP method
* Internal handler crash
* Missing files
* Bad Content-Length

Return:

400 Bad Request
404 Not Found
500 Internal Server Error

---

# 11. Configuration

File: config.py

Contains:

* HOST
* PORT
* BUFFER_SIZE
* STATIC_DIR
* DEBUG

---

# 12. Logging

Basic logging system:

* Print request method + path
* Log status code
* Log internal errors

Optional:
Add timestamp formatting.

---

# 13. Security Considerations

Even though educational:

### Must prevent:

* Directory traversal
* Infinite header injection
* Oversized bodies (limit size)
* Blocking forever on broken clients

Use:

* Request size limit
* Header size limit
* Timeout on socket

---

# 14. Testing Plan

### Manual testing via:

Browser:

```
http://localhost:8080/
```

Command line:

```
curl http://localhost:8080/
curl -X POST http://localhost:8080/submit -d "name=test"
```

Edge cases:

* Unknown route
* Large body
* Invalid HTTP

---

# 15. Stretch Features (Phase 2)

1. Keep-Alive connection support
2. Chunked transfer encoding
3. Async model with selectors
4. Basic middleware
5. WSGI adapter
6. Rate limiting
7. Thread pool instead of unlimited threads

---

# 16. Example Server Flow Walkthrough

Client:

```
GET / HTTP/1.1
Host: localhost
```

Server:

→ Accept connection
→ Parse GET + "/"
→ Router finds home handler
→ Handler returns text/html
→ Send response
→ Close connection

---

# 17. Deliverables

Codex must produce:

* Clean multi-file project
* No external frameworks
* No use of `http.server`
* Clear separation of components
* Fully working server
* Documentation comments

---

# 18. Performance Expectations

This server is:

* Single machine
* Educational
* Limited concurrency
* Not optimized for production

Expected:

~100–300 req/sec on simple GET

---

# 19. Why This Project Matters

This project builds deep understanding of:

* Networking fundamentals
* TCP vs HTTP layering
* Protocol parsing
* Server architecture
* Threading
* Resource management

This is foundational knowledge underlying production tools like:

* Flask
* Django
* FastAPI

Understanding these layers makes you stronger in:

* Backend engineering
* Systems design interviews
* Performance tuning
* Infrastructure debugging

---

# 20. Implementation Order (Critical)

Codex must implement in this order:

1. Basic TCP server
2. Minimal request parsing
3. Minimal response
4. Hardcoded route "/"
5. Router abstraction
6. Static files
7. POST handling
8. Threading
9. Error system
10. Cleanup + refactor

---

# 21. Final Acceptance Criteria

The server is complete when:

* Visiting `/` shows content.
* Visiting `/static/test.html` serves file.
* Unknown route returns 404.
* POST request returns body.
* Multiple browser tabs work simultaneously.
* No crashes on malformed input.
* Codebase is modular and readable.

---

