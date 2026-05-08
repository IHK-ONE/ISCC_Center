# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run commands from the project root (`/root/app`).

- Install runtime dependencies if they are missing: `python3 -m pip install flask requests urllib3`
- Run the Flask app: `python3 app.py`
- Check Python syntax: `python3 -m py_compile app.py`
- Check JavaScript syntax: `node --check static/js/app.js`

No dependency manifest, build step, lint configuration, or automated test suite is currently present in this repository.

## Architecture

This is a small Flask + vanilla JavaScript single-page management app for ISCC account, challenge, attachment, and flag workflows.

- `app.py` contains the entire backend: Flask routes, JSON persistence helpers, ISCC HTTP client logic, proxy handling, background operation tracking, and file download/delete handling.
- `templates/index.html` is the only server-rendered page. It mounts the client app at `#app` and loads `static/css/app.css` and `static/js/app.js`.
- `static/js/app.js` is a framework-free SPA. It keeps all client state in the top-level `state` object, renders tab content by assigning `innerHTML`, calls backend APIs through the shared `api()` helper, and polls progress/log endpoints for background work.
- `static/css/app.css` contains all UI styling; there is no CSS preprocessor or bundler.

## Backend structure

- Local data is stored as JSON files under `data/` and downloaded attachments under `downloads/`. These directories are created at startup by `ensure_runtime_dirs()`.
- Config, accounts, challenges, solves, files, and flags each have `default_*`, `load_*`, and `save_*` helpers. `save_json()` writes through a temp file and then replaces the target.
- Local admin auth uses `/api/setup`, `/api/login`, `/api/logout`, Flask sessions, and the `@require_auth` decorator. `safe_config()` returns config without exposing local auth password hashes.
- `ISCCClient` wraps the remote ISCC session: login, challenge listing/detail, solves, flag submission, and attachment downloading. Client instances are cached by account/config/proxy key.
- Proxy behavior is centralized in helpers such as `parse_proxy_list()`, `get_configured_proxy()`, `proxy_dict()`, `remove_failed_proxy()`, and `rebind_account_proxy()`. Failed proxies may be removed from config and surfaced through runtime logs.
- Long-running work uses `start_background_operation()` plus `OPERATION_PROGRESS`. Progress supports pause, resume, cancel, and skip through `/api/progress/<id>` endpoints.

## Main API areas

- Auth/status: `/api/status`, `/api/setup`, `/api/login`, `/api/logout`
- Accounts: `/api/accounts`, import, delete, and test-login endpoints
- Config/proxy: `/api/config`, `/api/config/proxy-test`
- Challenge sync and display: `/api/sync/run`, `/api/sync/challenges`, `/api/sync/solves`, `/api/challenges`
- Flags/submission: `/api/submit`, `/api/submit/batch`, `/api/flags`
- Attachments: `/api/files`, `/api/files/update`, delete/download file endpoints
- Runtime observability/control: `/api/logs`, `/api/progress`, pause/resume/cancel/skip endpoints

## Frontend structure

- `boot()` loads `/api/status`; authenticated sessions call `loadData()` and render the app, otherwise render the setup/login card.
- `pages` maps tab IDs to render functions for dashboard, accounts, challenges, files, submit, logs, and config.
- `loadData()`, `refreshLocalKind()`, and `refreshKinds()` keep client state synchronized with backend resources.
- `startProgress()` and `watchProgress()` create/poll operation progress cards; completion handlers refresh affected resources.
- Operation modals for challenge sync and attachment update are driven by `state.operationPanel`, then call `/api/sync/run` or `/api/files/update`.
- Most HTML is generated with template strings; use the existing `h()` helper for any dynamic text inserted into markup.
