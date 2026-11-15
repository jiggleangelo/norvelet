# norvelet

Prototype: Apparel and packaging order specs | norvelet

**Use it free, no account:** https://app-norvelet.manykiln.com/  
**Try the demo (made-up data):** https://try-norvelet.manykiln.com/  
**Product page:** https://norvelet.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 29 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# Spec-Checked Design-to-Order demo

A standard-library-only project for box and tee specifications. The original fictional concierge demonstration remains available through `demo.py`; the persistent self-service application is Norvelet.

Run the existing demo and all tests with Python 3.12:

```sh
python3 demo.py
python3 -m unittest discover -s tests -v
```

The existing demo remains intentionally fictional and keeps its original in-memory suppliers, order-intent flow, fee display, audit demonstration and tests. Nothing in the usable application changes `demo.py` or those modules.

## Usable app

Norvelet turns a real plain-English box and tee request into a checked specification sheet, illustrative RSC dieline, textile label and care drafts, claims checklist, copyable RFQ and user-entered supplier quote comparison.

It has no accounts, cookies, AI or LLM calls, payment flow, analytics, email collection, supplier messaging or outbound network requests. The quick-fill step uses the existing deterministic `FakeIntake` rules and is labelled in the interface as RuleParser-style rule-based quick-fill. Users must check every field.

Start it with:

```sh
APP_HOST=127.0.0.1 \
PORT=8080 \
PUBLIC_BASE_URL=https://app-norvelet.manykiln.com \
APP_HTTPS_TERMINATED=true \
DATA_DIR=/var/lib/norvelet \
APP_DATA_CAP_MB=200 \
python3 app.py
```

`DATA_DIR` is the application's only writable location. Workspaces are capability URLs: the random workspace token is the only key and is never stored on disk. The disk directory is its SHA-256 digest. A separate revocable token provides read-only sharing.

Each workspace can hold up to 50 projects. Each project retains up to 24 hash-chained versions and 20 supplier quotes. Workspace files are written atomically and survive restarts. Workspaces are limited to 5 MB, the global cap defaults to 200 MB, and writes are rejected clearly when a cap is reached. Unused workspaces expire after 24 hours; written workspaces expire after 90 idle days. A sweep runs at startup and daily.

The application accepts requests up to 1 MB, free text up to 4,000 characters and ordinary fields up to 512 characters. POST requests require a matching `Origin`, or an absent `Origin` with a matching `Referer`. The configured public authority is the only accepted Host. Responses use a same-origin referrer policy, CSP, `nosniff` and `no-store`; application logging is disabled.

The box SVG is an illustrative RSC template. A supplier's production dieline governs. Generated label and care copy must be checked against the actual blank and production process. The claims and labeling checklist is not legal advice. Children's-product requests are refused because CPSIA testing, certificates and tracking-label requirements are outside this tool.

Supplier quotes are entered by the user and are never fetched or sent. Totals use integer cents. Quotes retain the specification version against which they were entered and become visibly stale after a revision. Norvelet does not place orders or contact suppliers.

Useful endpoints:

- `GET /healthz` — unmetered liveness response.
- `POST /w` — create a private workspace.
- `POST /w/sample` — create a workspace containing fictional Fernleaf sample data.
- `GET /w/<token>/export` — download the complete workspace JSON.
- `POST /w/<token>/delete` — immediately remove the workspace and leave a deletion tombstone.
- `POST /w/<token>/share` — create or rotate a read-only share link.

Run the usable-app tests together with the unchanged suites:

```sh
python3 -m unittest tests.test_app -v
python3 -m unittest discover -s tests -v
```
