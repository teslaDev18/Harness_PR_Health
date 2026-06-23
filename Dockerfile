# PR Health Dashboard — container image
FROM python:3.11-slim

WORKDIR /app

# requests = live Harness reads; anthropic = AI fix suggestions.
RUN pip install --no-cache-dir requests anthropic

COPY pr_health.py ai_suggest.py notify.py sample_data.json ./

EXPOSE 8080

# Serve the dashboard. If a Harness token is present in the environment it reads
# live data; otherwise it falls back to the bundled sample data so the page
# always renders.
CMD ["sh", "-c", "python pr_health.py --serve --port 8080 $([ -n \"$HARNESS_API_KEY\" ] && echo --live)"]
