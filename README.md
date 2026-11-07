# Socket Chat

A Computer Network term project using Python, Tkinter, and TCP sockets. The planned application includes private chats, group chats, and four additional features.

**Current status:** The server and Tkinter client support connection management and ping. All 44 tests passed, including real TCP loopback and Tkinter UI tests. Accounts, chat, the SQL schema, and Docker images are not implemented yet.

## Run the server

Use Python 3.11 or later. The application uses the Python standard library and does not require additional pip packages. From the socket-chat directory:

```bash
python3 server.py --host 127.0.0.1 --port 5000
```

Press Ctrl-C to stop the server. For LAN connections, bind the server to `0.0.0.0` and allow access to its TCP port. See the local [Server guide](docs/server.md) for ping examples and the thread/queue flow.

## Run the Tkinter client

Open another terminal in the socket-chat directory:

```bash
python3 client.py --host 127.0.0.1 --port 5000
```

Select **Connect**, then **Send Ping**, and check Activity for the pong response. Select **Disconnect** to close the connection, or close the window to exit. Tkinter and a graphical display are required. When connecting from another computer, enter the server computer's LAN IP instead of `127.0.0.1`.

`client.py` owns the UI. `client_network.py` owns the socket and network thread. Requests and results pass through queues: the network module never imports Tkinter, and the UI never performs socket I/O. Both client and server receive data with `recv()` and assemble complete JSON lines with `LineFramer`.

This client slice does not poll automatically. If the server closes an idle connection, the client detects it on the next request. Reconnect explicitly with **Connect**. Requests are never retried automatically.

See the local [Client guide](docs/client.md) for details. The `docs/`, `diagram/`, and `GLOSSARY.md` paths are ignored by Git and exist only in the development workspace; they are not included in a Git checkout.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

GUI tests skip when no display is available. TCP tests require permission to open localhost sockets. Unicode fixtures use escape sequences in source files while still exercising multibyte UTF-8 data at runtime.

## Local documentation

The links in the following tables refer to ignored files available in the development workspace.

| Document | Purpose |
| --- | --- |
| [Requirements and acceptance criteria](docs/requirements.md) | R1-R11, grading, demonstration evidence, and submission materials |
| [Design overview](docs/design.md) | Agreed behavior and component responsibilities |
| [Implementation plan](docs/implementation-plan.md) | Milestones from connectivity to the two-computer demonstration |
| [Open decisions](docs/open-decisions.md) | Proposals and unresolved details, separate from confirmed decisions |
| [Implemented server](docs/server.md) | Running the server, testing ping, and understanding its execution flow |
| [Glossary](GLOSSARY.md) | Shared terminology for accounts, rooms, memberships, and pins |

## Detailed references and diagrams

| Topic | Documentation | PlantUML |
| --- | --- | --- |
| Client/server threads, queues, and responsibilities | [Design](docs/design.md) | [Architecture](diagram/architecture.puml) |
| Data structures | [Data model](docs/data-model.md) | [Database](diagram/database.puml) |
| TCP request/response | [Protocol](docs/protocol.md) | - |
| Action contracts and JSON examples | [API reference](docs/api-reference.md), [Examples](docs/protocol-examples.json) | - |
| Polling, history, and revisions | [Synchronization](docs/synchronization.md) | - |
| AI, hearts, pins, and a room-wide rimshot sound button | [Special features](docs/special-features.md) | - |
| OpenRouter and free models | [AI](docs/ai.md) | - |
| LAN, Docker, and persistent data | [Deployment](docs/deployment.md) | [Deployment](diagram/deployment.puml) |
| Assignment, template, and submission instructions | [References](docs/references/README.md) | - |

## Current project structure

```text
socket-chat/
|-- .gitignore
|-- README.md
|-- server.py                # Runnable transport/ping server
|-- client.py                # Tkinter UI and main-thread event handling
|-- client_network.py        # Socket worker and request/result queues
|-- common/
|   |-- __init__.py
|   `-- protocol.py          # Shared wire-format helpers
`-- tests/                   # Protocol, TCP, and Tkinter interaction tests
    |-- test_client_network.py
    |-- test_client_ui.py
    |-- test_protocol.py
    `-- test_server.py
```

The server and client are separate files as required by the assignment. The current server implements only ping and transport error responses. Known application actions without handlers return `ACTION_NOT_IMPLEMENTED`; this is not a complete implementation of Protocol v1.0.

Local documentation distinguishes **confirmed**, **proposed**, and **unresolved** decisions. Confirming a design does not prove that it has been implemented or tested.

Protocol v1.0 and the action contracts were specified by the assistant at the user's request as a shared API standard. Decisions outside that contract, such as the database engine, password storage, and AI model selector, remain in Open decisions.
