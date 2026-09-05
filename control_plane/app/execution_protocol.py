from __future__ import annotations

from .models import Task


EXECUTION_METADATA_KEY = "ai_orchestra_execution_id"


def execution_message_id(execution_id: str) -> str:
    """Return a stable OpenCode message id for one logical execution dispatch."""
    compact = execution_id.replace("-", "")
    return f"msg_orchestra_{compact}"


def execution_session_title(task: Task, execution_id: str) -> str:
    return f"AI Orchestra · {task.title[:70]} · {execution_id}"


def execution_prompt(task: Task) -> str:
    return f"""Ты руководитель виртуального отдела разработки AI Orchestra.

Выполни задачу как руководитель отдела: декомпозируй, при необходимости делегируй профильным агентам, организуй независимую QA-проверку и верни руководителю итог.

Задача: {task.title}
Проект: {task.project}
Направление: {task.domain}
Приоритет: {task.priority}
Риск: {task.risk_level}

Описание и критерии приемки:
{task.description or "Дополнительное описание не задано."}

Ограничения:
- не выполняй production deploy;
- не делай git push;
- не запрашивай и не раскрывай секреты;
- не выполняй внешнюю запись или финансовые операции;
- если действие требует такого разрешения, остановись и явно укажи требуемое согласование;
- в финале дай краткое резюме, выполненные проверки, измененные файлы/артефакты и открытые риски.
"""
