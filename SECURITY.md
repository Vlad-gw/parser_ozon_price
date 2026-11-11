# Security notes

This is a personal-use project. **Do not deploy it directly to the public internet without additional hardening.**

- Keep `.env`, Telegram token, chat ID, browser profiles, cookies and `data/` outside Git.
- Rotate the Telegram bot token immediately if you accidentally commit or disclose it. Deleting a file from the latest commit does not erase earlier Git history.
- The local startup script binds FastAPI to `127.0.0.1`. For public access use HTTPS, a reverse proxy and a strong `APP_PASSWORD`; review authentication and rate limiting for your deployment.
- Never expose Chrome DevTools Protocol (`9223`) to a public interface or untrusted network.
- Make SQLite backups using `backup_db.py` / `backup.command` rather than copying an active `.sqlite3` file in WAL mode.
- Ozon may rate-limit, challenge or block automated access. Do not treat a failed request as a product discount.

Report security concerns privately to the repository owner rather than opening a public issue containing credentials or personal data.
