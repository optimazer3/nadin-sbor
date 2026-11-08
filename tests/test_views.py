from datetime import date

from bot.models import Status, Task, TaskDraft
from bot.views import dashboard, draft_text, split_message, task_list_messages

TODAY = date(2026, 11, 9)  # понедельник


def make(id, title, assignee=None, deadline=None, status=Status.NEW):
    return Task(id=id, title=title, assignee=assignee, deadline=deadline, status=status)


def test_dashboard_empty():
    [msg] = dashboard([make(1, "Готово", status=Status.DONE)], TODAY)
    assert "Активных задач нет" in msg


def test_dashboard_groups_and_flags():
    tasks = [
        make(1, "Отчёт", "Иван", date(2026, 11, 5), Status.IN_PROGRESS),  # просрочено
        make(2, "Прайс", "Мария", date(2026, 11, 9)),  # сегодня
        make(3, "Звонок", "Иван", date(2026, 11, 12)),
        make(4, "Без срока", None, None, Status.ON_HOLD),
        make(5, "Закрыто", "Иван", None, Status.DONE),  # не активная
        make(6, "<script>", "Мария"),
    ]
    [msg] = dashboard(tasks, TODAY)
    assert "Активных задач: <b>5</b>" in msg
    assert "❗ Просрочено: <b>1</b>" in msg
    assert "⏰ Срок на этой неделе: 2" in msg
    assert "Без срока: 2" in msg
    assert "Закрыто" not in msg
    assert "&lt;script&gt;" in msg and "<script>" not in msg
    # Иван раньше Марии, «Без исполнителя» в конце; внутри группы — по сроку.
    assert msg.index("👤 <b>Иван</b> (2)") < msg.index("👤 <b>Мария</b> (2)") < msg.index("Без исполнителя")
    assert msg.index("№1 Отчёт") < msg.index("№3 Звонок")
    assert "просрочено на 4 дня" in msg
    assert "№2 Прайс · до 09.11 ⏰ сегодня" in msg


def test_dashboard_splits_long_output():
    tasks = [make(i, "Очень длинная задача " * 10, f"Сотрудник {i % 7}") for i in range(1, 120)]
    messages = dashboard(tasks, TODAY)
    assert len(messages) > 1
    assert all(len(m) <= 4000 for m in messages)
    joined = "\n".join(messages)
    assert all(f"№{i} " in joined for i in range(1, 120))


def test_split_message_respects_limit():
    blocks = ["a" * 30, "b" * 30, "c\n" * 40]
    out = split_message(blocks, limit=50)
    assert all(len(m) <= 50 for m in out)
    assert "".join(out).count("c") == 40


def test_draft_text_single_and_multi():
    one = draft_text([TaskDraft("Отчёт", "Иван", date(2026, 11, 13))], TODAY, recognized="Ивану отчёт")
    assert "Вы сказали" in one and "Я правильно понял задачу?" in one
    assert "13.11.2026 (пт) — через 4 дня" in one
    many = draft_text([TaskDraft("A"), TaskDraft("B")], TODAY, recognized=None)
    assert "Задач: 2" in many and "<b>2.</b>" in many and "Вы сказали" not in many


def test_task_list_messages():
    tasks = [
        make(7, "Звонок", None, None, Status.ON_HOLD),
        make(3, "Отчёт", "Иван", date(2026, 11, 5), Status.IN_PROGRESS),
    ]
    [msg] = task_list_messages(tasks, TODAY, "📋 <b>Задачи</b>")
    assert msg.startswith("📋 <b>Задачи</b> — 2")
    assert msg.index("№3") < msg.index("№7")  # со сроком — выше
    assert "👤 Иван · 📅 до 05.11 ❗ просрочено на 4 дня · 🔄 В работе" in msg
    assert "👤 исполнитель не указан · ⏸ На паузе" in msg
    assert "«задача 3 выполнена»" in msg
    assert task_list_messages([], TODAY, "T") == ["T\n\nЗадач нет."]


def test_task_list_splits_long_lists():
    tasks = [make(i, "Длинная задача " * 15, "Иван") for i in range(1, 80)]
    messages = task_list_messages(tasks, TODAY, "T")
    assert len(messages) > 1 and all(len(m) <= 4000 for m in messages)
