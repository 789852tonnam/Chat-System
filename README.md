# Socket Chat

A Computer Network term project using Python, Tkinter, and TCP sockets. The planned application includes private chats, group chats, and four additional features.

**Current status:** The server and Tkinter client support username-only entry, persistent user IDs, a live online directory, private and group chat with persistent history, message hearts, a single room pin, AI summaries, room sound playback, connection management, and ping. All 190 tests passed, including SQLite persistence, real TCP loopback, and Tkinter UI tests. Docker images and the two-physical-computer demonstration remain pending.

## Run the server

Use Python 3.11 or later. The Python code uses the standard library and requires no additional pip packages. Sound playback on clients requires an installed FFmpeg build with `ffplay` on PATH; the server does not need it. From the socket-chat directory:

```bash
python3 server.py --host 127.0.0.1 --port 5000
```

The server creates `data/chat.sqlite3` for persistent users. Choose another file with `--database PATH`, or use `--database :memory:` for temporary storage. The runtime `data/` directory is ignored by Git.

Press Ctrl-C to stop the server. For LAN connections, bind the server to `0.0.0.0` and allow access to its TCP port. See the local [Server guide](docs/server.md) for ping examples and the thread/queue flow.

For AI summaries, copy [.env.example](.env.example) to `.env` beside `server.py` and fill `OPENAI_BASE_URL`, `OPENAI_MODEL` and `OPENAI_API_KEY`. These names configure an OpenAI-compatible API; OpenRouter is only the example provider. Use a base URL such as `https://openrouter.ai/api/v1`, without `/chat/completions`. The server loads the file at startup; exported environment values override it. Missing credentials disable summaries while chat stays available. `.env` is ignored by Git, and the key is never sent to chat clients or included in logs. Restart the server after changing configuration.

## Run the Tkinter client

Open another terminal in the socket-chat directory:

```bash
python3 client.py --host 127.0.0.1 --port 5000
```

Enter a username, select **Join**, then **Send Ping**, and check Activity for the pong response. No registration or password is required. Select **Disconnect** to close the connection, or close the window to exit. Tkinter and a graphical display are required. When connecting from another computer, enter the server computer's LAN IP instead of `127.0.0.1`.

`client.py` owns widgets, input reading, rendering and scrolling. `client_logic.py` owns validation, connection/login state, room/message data, history cursors, request ordering and polling. UI and logic run on the same main thread: button callbacks pass plain values to logic, and logic emits synchronous display events. `client_network.py` owns the existing socket thread and exchanges requests/results with logic through queues. Logic and network never import Tkinter, and the UI never performs socket I/O. Both client and server receive data with `recv()` and assemble complete JSON lines with `LineFramer`.

The online list includes your own name, marked `(you)`, and shows each user once even when that identity has multiple connections. It refreshes immediately after Join, then every 3 seconds after a snapshot returns. Polls wait for any request already in flight and never accumulate. While refreshing, Ping is temporarily disabled; Disconnect and window close remain available. Selection is preserved for users who remain online.

Logging out or closing the last connection removes that user from later snapshots. Disconnected sessions are removed by the server worker before requests and during idle maintenance; stored users remain in SQLite. Background polling keeps healthy idle clients active. If a connection fails, the client reports the error and clears the online list. Reconnect explicitly with **Join**. Requests are never retried automatically.

See the local [Client guide](docs/client.md) for details. The `docs/`, `diagram/`, and `GLOSSARY.md` paths are ignored by Git and exist only in the development workspace; they are not included in a Git checkout.

## Private chat

Select another online user and choose **Open Chat**. Type a message and choose **Send**, or press **Ctrl-Enter**. Enter inserts a newline. Both participants open the same private room; the server checks membership for every read and send. A third user cannot access that room by supplying its key.

Opening a room loads its latest 20 messages. **Load older (20)** fetches the next older batch. The active room polls every second, with one request in flight across directory, history, and user actions. Messages use global increasing IDs, so room histories may contain gaps. Catch-up batches are limited to 100 messages and continue from the last delivered ID.

Only the active room is cached in client memory. Switching rooms clears that cache, fetches history again, and ignores replies belonging to an earlier room generation. Send acknowledgements do not append messages or advance the history cursor; the next sync supplies the complete ordered update. A send error retains the input for correction. Private messages remain stored when the other participant is offline, and rooms, memberships, and messages survive a server restart using the same database.

Human and AI messages share history, hearts and pins. The room sound button uses an independent transient revision. Hearts and pins are persisted and synchronized through room revisions and paginated snapshots.

## Group conversations

Enter a **New group name** and choose **Create**. Names are trimmed to 1-80 Unicode characters, with no controls or line breaks; duplicate names are allowed because each group has a unique UUID key. A new group starts with its creator as its only member and opens automatically.

Every logged-in user can see all groups. Select one to view its public member names, then explicitly choose **Join / Open** to join or reopen it. Selecting a group alone does not reveal its messages. Existing members can use the same button without creating duplicate memberships. Creating or joining refreshes the directory immediately; other clients see membership changes through the normal 3-second directory poll.

Members can read the group's earlier history and send messages. The server rejects non-member reads/writes even when a caller knows the key. Membership and history survive logout and server restarts. Group history uses the same latest-20, older-20 and active-room polling behavior as private chats; changing rooms clears the previous message cache.

## Hearts and pins

Click a message to select it, or place the chat cursor on it and press Enter. Choose **Heart** / **Unheart** or **Pin message**. Heart counts are shown beside each message; a filled heart marks this account's reaction. The same account on several devices counts once.

Each room has one pinned message in a scrollable banner above history. Any member can replace it or choose **Unpin**, including a pin set by another member. A pin can remain visible even when its message is outside the loaded history. Removing a pin keeps the original message.

Actual heart/pin changes advance one persistent `room_revision`; repeated desired-state requests do not. A changed revision reloads the entire displayed range in pages of at most 100, stages pages until they share a revision, then updates the UI while retaining history, selection and scroll. Mutation acknowledgements never advance the cached revision or message cursors. If the room changes twice while reloading, the client retains its old view and retries on the next poll.

Existing database files are upgraded automatically at server startup by adding the heart table and missing pin column. Users, rooms, memberships and messages are retained. Heart/pin state survives restart. Non-members cannot mutate or read snapshots.

## AI summaries

Open a conversation and choose **Summarize**. The server selects up to 50 latest human messages, copies them in chronological order and acknowledges a new job immediately. Each request runs on its own API thread, so chatting and other summary requests continue while it waits. Previous AI summaries are excluded from the input.

Successful output appears as an **AI** message for every member through normal polling. The database retains the actual returned model, job ID and last human source ID. AI messages have no human sender account and can be hearted or pinned. Provider failures appear only to the requester in Activity, without creating error messages in the room. Running jobs are checked every 3 seconds.

A socket wait times out after 30 seconds and the worker enforces a 60-second job deadline. Late or duplicate results are not saved. Completed job statuses expire after an hour or server restart; successful AI messages remain in history. Requests are never automatically retried or switched to another model. In-flight HTTPS cannot be interrupted by urllib: daemon API threads may finish after server shutdown, and their results are discarded.

## One room sound

Each client needs [FFmpeg/ffplay](https://ffmpeg.org/ffplay.html) on PATH and the bundled [SFX/sfx1.mp3](SFX/sfx1.mp3). Open a room and choose **Play Rimshot**. There is one button and one sound, with no sound selector or message selection required. It also works in an empty room.

The server checks membership and advances a per-room RAM counter. Active-room polling carries the latest counter; every client viewing that room, including the requester, plays its local copy once when it changes. The acknowledgement itself does not play sound. Opening/reopening a room silently establishes a baseline, and server restart resets counters. Multiple presses between polls can collapse to one playback, and polling does not guarantee simultaneous starts on different machines.

Playback launches asynchronously. A new event restarts the same clip rather than overlapping it. Leaving the room stops its clip; closing the window kills/reaps owned player processes. Missing ffplay, a missing asset or a failed player is reported in Activity while chat remains usable. There is no automatic replay after a playback error; press again after fixing the local player. The MP3 is bundled with each client, while only the event travels through chat sockets. Sound adds no database messages or heart/pin revisions.

## Username identity

Usernames contain 3-32 ASCII letters, digits, or underscores, without spaces. The first join creates the user; later joins match case-insensitively and reuse its ID while retaining its original spelling. IDs survive server restarts when the same database file is used. Multiple connections may use the same identity, as previously agreed.

This is identification by name, not password authentication: anyone entering an existing username can use that identity. The server binds the chosen user to its connection and does not accept a user ID supplied by the client as proof of identity.

`login` accepts only `{"username": "Alice"}`. `logout` clears the identity on its current connection; it does not delete the database user. `register` is no longer a supported action.

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

Automated audio tests mock player processes and do not make noise. AI tests use mocked providers and temporary environment files, without loading the real `.env` or making external requests. Client logic tests run without a display or TCP sockets. GUI tests skip when no display is available. TCP tests require permission to open localhost sockets. Unicode fixtures use escape sequences in source files while still exercising multibyte UTF-8 data at runtime.

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
|-- .env.example             # OpenAI-compatible base URL, model and API key
|-- .gitignore
|-- README.md
|-- server.py                # Lifecycle, queue, application worker and handlers
|-- server_ai.py             # Private configuration and OpenAI-compatible HTTP
|-- server_network.py        # TCP listener, connection threads and framing
|-- database.py              # Worker-owned SQLite users, rooms and messages
|-- client.py                # Tkinter widgets, callbacks and rendering
|-- client_audio.py          # Fixed local MP3 playback and process cleanup
|-- client_logic.py          # Application state, actions and polling
|-- client_network.py        # Socket worker and request/result queues
|-- SFX/
|   `-- sfx1.mp3              # The single user-provided room sound
|-- common/
|   |-- __init__.py
|   `-- protocol.py          # Shared wire-format helpers
`-- tests/                   # Protocol, headless logic, TCP and Tkinter tests
    |-- test_chat.py
    |-- test_client_audio.py
    |-- test_client_logic.py
    |-- test_client_network.py
    |-- test_client_ui.py
    |-- test_directory.py
    |-- test_effects.py
    |-- test_groups.py
    |-- test_login.py
    |-- test_protocol.py
    |-- test_reactions.py
    |-- test_server.py
    |-- test_server_ai.py
    `-- test_summaries.py
```

Server responsibilities are split explicitly: `server_network.py` owns accept/recv/send, framing and connection deadlines; `server.py` owns the bounded work queue, sessions, application handlers and the SQLite worker. Transport calls `submit(request, session)` and returns its response on the same connection. Both share a shutdown Event so pending response waits can exit. Startup initializes storage before opening the listener; shutdown closes transport before joining the database worker.

The server and client are separate files as required by the assignment. The current server implements ping, username-only login, logout, get_directory, open_private, create_group, join_group, send_message, sync_room, get_room_snapshot, set_heart, set_pin, request_summary, get_summary_status, trigger_effect, and transport error responses. The directory returns all groups with their members, including groups the caller has not joined. All 15 protocol actions now have handlers; unknown actions return `UNKNOWN_ACTION`. Deployment and assignment demonstration checks remain separate from local implementation tests.

Local documentation distinguishes **confirmed**, **proposed**, and **unresolved** decisions. Confirming a design does not prove that it has been implemented or tested.

Protocol v1.1 and the action contracts were specified by the assistant at the user's request as a shared API standard. Decisions outside that contract, such as Docker packaging and demonstration setup, remain in Open decisions.
