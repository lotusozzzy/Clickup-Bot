# Temporary SMTP TLS fixtures

The SMTP tests require the `openssl` CLI on `PATH`, plus the application's existing
Python dependencies. A missing CLI fails with an explanatory error; TLS coverage
is never silently skipped. Run `python3 -m unittest discover -s tests -v` from the
repository root.

Each test-suite run generates a fresh RSA-2048/SHA-256 CA and a server certificate
for the reserved test domain `smtp.fixture.test`, valid for two days. Both private
keys, certificates, and configuration files stay in a private temporary directory
that is removed after the suite. No certificate or private-key blobs are stored
in this repository. Each local generation command has a 30-second timeout.

Tests exchange real TLS records through Python's `ssl.MemoryBIO`, without network
connections or real credentials. The CA is added only to disposable test contexts,
never to the system trust store. These generated materials are for tests only.
