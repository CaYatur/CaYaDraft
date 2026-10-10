# Security

CaYaDraft runs on your machine and can push branches when the configured owner asks. Treat the GitHub private key, the personal access token, and `WEBHOOK_SECRET` as secrets. Do not commit them.

Install the GitHub App only on your account. A public webhook would send other people's issues to your process.

The bot reads issue text and sends it to the local Ollama model only. It does not execute text from an issue as a command, and it does not run the repository's scripts or tests. It can edit a text file or add one. Secret-looking paths such as `.env` and private keys are refused.

Report a problem in this repository's issues, or privately if the report includes a live token or webhook secret.
