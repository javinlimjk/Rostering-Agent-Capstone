import os
from celery import Celery

# Compute absolute path for celery sqlite db
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
celery_db = os.path.join(base_dir, "data", "celery_broker.db")
celery_results = os.path.join(base_dir, "data", "celery_results.db")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# For local fallback when redis is missing
BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

# Check if we should fallback to sqlite (e.g. if we are local and redis is not running)
# This is a bit of a hack for the environment
if "localhost" in REDIS_URL and os.system("redis-cli ping > /dev/null 2>&1") != 0:
    BROKER_URL = f"sqla+sqlite:///{celery_db}"
    RESULT_BACKEND = f"db+sqlite:///{celery_results}"

celery_app = Celery(
    "roster_tasks",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks"]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)
