from aiogram import Router, F
from aiogram.filters import Command
from aiogram.enums import ChatType, ChatAction
import math
import asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import json
import time
from datetime import datetime, timezone

from bot.states import ScreeningStates
from config import settings
from services.llm_scoring import score_candidate
from services.sheets import append_row, fetch_rows

router = Router()

# Premium "Thanos" for admin: track recently sent admin messages (menu + stats) per admin user
_ADMIN_MSG_IDS: dict[int, list[tuple[int, int]]] = {}  # user_id -> [(chat_id, message_id), ...]
_ADMIN_MSG_LIMIT = 6  # how many recent admin messages to remember/delete


def _track_admin_msg(user_id: int, chat_id: int, message_id: int) -> None:
    buf = _ADMIN_MSG_IDS.get(user_id, [])
    buf.append((chat_id, message_id))
    if len(buf) > _ADMIN_MSG_LIMIT:
        buf = buf[-_ADMIN_MSG_LIMIT:]
    _ADMIN_MSG_IDS[user_id] = buf


async def _thanos_delete(bot, user_id: int) -> None:
    items = _ADMIN_MSG_IDS.get(user_id, [])
    # delete in reverse order (newest first)
    for chat_id, msg_id in reversed(items):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except TelegramBadRequest:
            pass
        except Exception:
            pass
    _ADMIN_MSG_IDS[user_id] = []


def _stars_0_10(score: int) -> str:
    # 0..10 -> 0..5 stars (9->5, 8->4, 7->4, 6->3, ...)
    s = max(0, min(10, int(score)))
    # Use ceil so 9/10 -> 5 stars (premium UX)
    n = int(math.ceil(s / 2))
    return "⭐" * n if n > 0 else "—"


def _material_line(project_link: str | None) -> str:
    t = (project_link or "").strip().lower()
    if t in {"nda", "n/a", "na"}:
        return "NDA (ссылка в анкете)."
    if t in {"declined", "не хочу", "нехочу", "skip", "нет"}:
        return "Отказался делиться ссылкой."
    if (project_link or "").startswith("http://") or (project_link or "").startswith("https://"):
        return project_link or ""
    return (project_link or "—").strip() or "—"


def _ai_first_label(overall: int) -> str:
    # 0..10 -> verbal label
    if overall >= 8:
        return "сильное"
    if overall >= 5:
        return "устойчивое"
    return "ограниченное"


def _top_strength_ru(c1: int | None, c2: int | None, c3: int | None) -> str:
    # Return RU label of the strongest criterion (no English text)
    items = [
        ("практический опыт", c1),
        ("контроль и логика", c2),
        ("продуктовый подход", c3),
    ]
    items = [x for x in items if isinstance(x[1], int)]
    if not items:
        return "сильные стороны"
    items.sort(key=lambda x: x[1], reverse=True)
    return items[0][0]


def _shorten(text: str, max_len: int) -> str:
    t = (text or "").strip().replace("\n", " ")
    if len(t) <= max_len:
        return t
    return t[: max_len - 3].rstrip() + "..."


def _pick_signal(crit: list, idx: int, fallback: str) -> str:
    """
    Take a short, human fragment from rationale to make the summary feel personal,
    while keeping your fixed style.
    """
    try:
        if not (isinstance(crit, list) and len(crit) == 3):
            return fallback
        r = str(crit[idx].get("rationale", "")).strip().replace("\n", " ")
        if not r:
            return fallback
        # keep it short and "quote-like"
        r = _shorten(r, 110)
        return r
    except Exception:
        return fallback


def _is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == settings.admin_user_id)


def _admin_entry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Админ-панель", callback_data="admin:menu")]
    ])


def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="admin:all")],
        [InlineKeyboardButton(text="🔥 Топ-кандидаты", callback_data="admin:top")],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="admin:close")],
    ])


async def _render_admin_stats(only_top: bool) -> str:
    rows = fetch_rows()
    if not rows:
        return "📊 Статистика\nПока нет прохождений."

    def _to_int(x: str, default: int = 0) -> int:
        try:
            return int(str(x).strip())
        except Exception:
            return default

    def _to_bool(x: str) -> bool:
        s = str(x).strip().lower()
        return s in ("true", "1", "yes", "y", "да")

    norm = []
    for r in rows:
        top_candidate = _to_bool(r.get("top_candidate", ""))
        if only_top and not top_candidate:
            continue
        overall = _to_int(r.get("overall_score", "0"), 0)
        scoring_failed = _to_bool(r.get("scoring_failed", "false"))
        username = (r.get("username") or "").strip()
        full_name = (r.get("full_name") or "").strip()
        display = f"@{username}" if username else (full_name or "unknown")
        ts = (r.get("timestamp_utc_iso") or "").strip()
        norm.append(
            {
                "top_candidate": top_candidate,
                "overall": overall,
                "scoring_failed": scoring_failed,
                "display": display,
                "ts": ts,
            }
        )

    if not norm:
        return "📊 Статистика (топ)\nПока нет топ-кандидатов."

    total = len(norm)
    top_count = sum(1 for x in norm if x["top_candidate"])
    failed_count = sum(1 for x in norm if x["scoring_failed"])
    avg = round(sum(x["overall"] for x in norm) / max(total, 1), 1)

    top3 = sorted(norm, key=lambda x: (x["overall"], x["ts"]), reverse=True)[:3]
    title = "📊 Статистика (топ)" if only_top else "📊 Статистика"
    lines = [
        title,
        f"Всего прохождений: {total}",
        f"Средний балл: {avg}",
        f"Топ-кандидаты: {top_count}",
        f"LLM errors: {failed_count}",
        "",
        "🏆 Топ-3:",
    ]
    for i, t in enumerate(top3, start=1):
        badge = "🔥 " if t["top_candidate"] else ""
        lines.append(f"{i}) {badge}{t['display']} — {t['overall']}/10")
    return "\n".join(lines)


QUESTIONS = [
    (
        "Вопрос 1.\nРасскажи о любой рабочей задаче, которую ты решил с помощью AI. Что именно ты ему делегировал и какой результат получил на выходе?"
    ),
    (
        "Вопрос 2.\nОпиши логику своего самого эффективного промпта или цепочки (chain). Как ты проверял, что AI выдает корректный и валидный ответ, а не просто правдоподобный текст?"
    ),
    (
        "Вопрос 3.\nБыл ли в твоей практике случай, когда AI выдал критическую ошибку или «галлюцинацию»? Как ты изменил подход или архитектуру, чтобы минимизировать такие риски в будущем?"
    ),
    (
        "Вопрос 4.\nКак ты объективно оцениваешь качество работы AI в продукте? На какие 2–3 метрики или сигнала ты смотришь в первую очередь, чтобы понять, что фича работает как надо?"
    ),
    (
        "Вопрос 5.\nЕсли нужно собрать и запустить работающую AI-фичу всего за 24 часа, на чем ты сэкономишь время, а какие этапы контроля качества оставишь обязательными?"
    ),
    (
        "Вопрос 6.\nКак бы ты организовал работу с LLM, если данные проекта нельзя передавать во внешние облачные API? Опиши стек или архитектурный подход, который ты бы выбрал в этом случае."
    ),
]


def _norm(text: str | None) -> str:
    return (text or "").strip()


def _is_decline(text: str) -> bool:
    t = text.lower().replace(" ", "")
    return t in {"нехочу", "не хочу", "declined", "нет", "skip"}


def _is_nda_word(text: str) -> bool:
    t = (text or "").strip().lower()
    t_compact = t.replace(" ", "")
    return t_compact in {"nda", "ндав", "подnda", "подnda:", "подnda.", "подnda!", "подnda?"} or t in {"nda", "под nda", "под нда"}


def _looks_like_domain_without_scheme(text: str) -> bool:
    """
    Detect inputs like 'github.com', 'google.com', 'site.ru/path' without http(s)://
    to force proper URL formatting (as per validation requirement).
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith("http://") or t.startswith("https://"):
        return False
    # if contains spaces -> it's likely a description, not a bare domain
    if " " in t:
        return False
    # basic domain markers
    if "." in t and any(t.endswith(suf) for suf in (".com", ".net", ".org", ".io", ".ai", ".ru", ".dev", ".app", ".me", ".co")):
        return True
    # also allow paths like github.com/user/repo
    if ".com/" in t or ".ru/" in t or ".io/" in t or ".ai/" in t or ".dev/" in t:
        return True
    return False


def _is_reasonable_nda_note(text: str) -> bool:
    """
    Accept NDA note only if it looks like a short description (not a single word/domain).
    """
    t = (text or "").strip()
    # Allow short NDA notes too (demo-friendly), but still reject single-word junk.
    # Examples accepted: "nda: делал RAG для юр. поиска", "NDA много делал"
    if not t:
        return False
    words = [w for w in t.replace("\n", " ").split(" ") if w.strip()]
    # Reject very short / meaningless strings like "ok" / "nda" (handled separately in link_handler)
    if len(t) < 8:
        return False
    return len(words) >= 2


def _is_valid_http_url(text: str) -> bool:
    t = text.strip()
    return t.startswith("http://") or t.startswith("https://")


def _rules_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Погнали", callback_data="go:start")]
    ])


async def _send_rules(message: Message) -> None:
    # Rules should be readable and not instantly replaced by Q1.
    # Keep as a normal message; "one-screen replace" starts from Q1.
    await message.answer(
        "Привет! Это короткий HR-скрининг (5 вопросов, ~3–5 минут).\n"
        "Правила: можно остановить в любой момент командой /cancel, "
        "перезапустить — /restart. Ответы используются только для оценки.\n"
        "Готов? Жми кнопку ниже 🙂",
        reply_markup=_rules_kb(),
    )


async def _send_replace(message: Message, state: FSMContext, text: str, reply_markup=None, parse_mode=None) -> None:
    """
    UX: keep chat clean — delete previous bot question and send the next one.
    This guarantees the next question appears AFTER the user's answer (no confusion).
    """
    data = await state.get_data()
    last_id = data.get("last_bot_msg_id")
    await asyncio.sleep(0.9)
    if last_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=int(last_id))
        except (TelegramBadRequest, ValueError, TypeError):
            pass
        except Exception:
            pass

    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    await state.update_data(last_bot_msg_id=sent.message_id)


async def _transition_accepted(message: Message, state: FSMContext, q_no: int) -> None:
    """
    Premium UX: show which question was accepted before switching to the next one,
    so the user doesn't feel like they answered the wrong question.
    """
    data = await state.get_data()
    last_id = data.get("last_bot_msg_id")
    if not last_id:
        await asyncio.sleep(0.9)
        return
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=int(last_id),
            text=f"✅ Ответ на Вопрос {q_no} принят. Следующий вопрос…",
            parse_mode=None,
            reply_markup=None,
        )
    except Exception:
        pass
    await asyncio.sleep(0.9)


async def _ask_q(message: Message, state: FSMContext, idx: int) -> None:
    await _send_replace(message, state, QUESTIONS[idx], parse_mode=None)


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ScreeningStates.rules)
    await _send_rules(message)
    # Q1 starts only after user presses "Погнали"


@router.message(Command("restart"))
async def restart_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ScreeningStates.rules)
    await _send_rules(message)
    # Q1 starts only after user presses "Погнали"


@router.callback_query(F.data == "go:start")
async def go_start_cb(callback: CallbackQuery, state: FSMContext) -> None:
    # Only proceed for users who are in rules state (avoid random clicks)
    cur = await state.get_state()
    if cur != ScreeningStates.rules.state:
        await callback.answer("Ок", show_alert=False)
        return

    # Premium UX: remove button immediately + delete the intro message
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    # init flow after cleaning the chat
    # IMPORTANT: do NOT send Q1 via send_message напрямую — иначе last_bot_msg_id не сохранится
    await state.set_state(ScreeningStates.q1)
    await state.update_data(answers={}, last_bot_msg_id=None)

    # send first question using replace-UX (tracks last_bot_msg_id from Q1)
    await _ask_q(callback.message, state, 0)

    await callback.answer()


@router.message(Command("admin"))
async def admin_handler(message: Message) -> None:
    if not _is_admin(message):
        await message.answer("⛔️ Команда доступна только администратору.")
        return

    # Optional filter: "/admin top"
    only_top = False
    if message.text:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip().lower() in ("top", "топ"):
            only_top = True
    try:
        text = await _render_admin_stats(only_top=only_top)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось прочитать Google Sheet: {str(e)[:300]}", parse_mode=None)
        return
    await message.answer(text, parse_mode=None)


@router.message(Command("chatid"))
async def chatid_handler(message: Message) -> None:
    if not _is_admin(message):
        await message.answer("⛔️ Команда доступна только администратору.")
        return
    # Useful when setting ADMIN_CHAT to a group/supergroup id
    chat_type = message.chat.type if message.chat else "unknown"
    await message.answer(f"chat_id: {message.chat.id}\nchat_type: {chat_type}", parse_mode=None)


@router.callback_query(F.data == "admin:menu")
async def admin_menu_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != settings.admin_user_id:
        await callback.answer("⛔️ Только для администратора.", show_alert=True)
        return
    m = await callback.message.answer("Админ-панель:", reply_markup=_admin_menu_kb())
    _track_admin_msg(settings.admin_user_id, m.chat.id, m.message_id)
    await callback.answer()


@router.callback_query(F.data == "admin:all")
async def admin_all_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != settings.admin_user_id:
        await callback.answer("⛔️ Только для администратора.", show_alert=True)
        return
    try:
        text = await _render_admin_stats(only_top=False)
    except Exception as e:
        m = await callback.message.answer(f"⚠️ Не удалось прочитать Google Sheet: {str(e)[:300]}", parse_mode=None)
        _track_admin_msg(settings.admin_user_id, m.chat.id, m.message_id)
        await callback.answer()
        return
    m = await callback.message.answer(text, parse_mode=None)
    _track_admin_msg(settings.admin_user_id, m.chat.id, m.message_id)
    await callback.answer()


@router.callback_query(F.data == "admin:top")
async def admin_top_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != settings.admin_user_id:
        await callback.answer("⛔️ Только для администратора.", show_alert=True)
        return
    try:
        text = await _render_admin_stats(only_top=True)
    except Exception as e:
        m = await callback.message.answer(f"⚠️ Не удалось прочитать Google Sheet: {str(e)[:300]}", parse_mode=None)
        _track_admin_msg(settings.admin_user_id, m.chat.id, m.message_id)
        await callback.answer()
        return
    m = await callback.message.answer(text, parse_mode=None)
    _track_admin_msg(settings.admin_user_id, m.chat.id, m.message_id)
    await callback.answer()


@router.callback_query(F.data == "admin:close")
async def admin_close_cb(callback: CallbackQuery) -> None:
    if not callback.from_user or callback.from_user.id != settings.admin_user_id:
        await callback.answer("⛔️ Только для администратора.", show_alert=True)
        return
    # Track the menu message itself so it also disappears
    try:
        _track_admin_msg(settings.admin_user_id, callback.message.chat.id, callback.message.message_id)
    except Exception:
        pass
    await _thanos_delete(callback.bot, settings.admin_user_id)
    await callback.answer("Закрыто ✅", show_alert=False)


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, остановил. Если захочешь начать заново — /start")


@router.message(ScreeningStates.q1)
async def q1_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Коротко: напиши пару предложений (можно без деталей).")
        return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q1"] = text
    await state.update_data(answers=answers)
    await _transition_accepted(message, state, 1)
    await state.set_state(ScreeningStates.q2)
    await _ask_q(message, state, 1)


@router.message(ScreeningStates.q2)
async def q2_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Ок, но нужно хоть 1–2 строки. Как именно использовал AI?")
        return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q2"] = text
    await state.update_data(answers=answers)
    await _transition_accepted(message, state, 2)
    await state.set_state(ScreeningStates.q3)
    await _ask_q(message, state, 2)


@router.message(ScreeningStates.q3)
async def q3_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Если не было — так и напиши: \"не было\".")
        return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q3"] = text
    await state.update_data(answers=answers)
    await _transition_accepted(message, state, 3)
    await state.set_state(ScreeningStates.q4)
    await _ask_q(message, state, 3)


@router.message(ScreeningStates.q4)
async def q4_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Можно без цифр, но нужен пример \"до/после\".")
        return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q4"] = text
    await state.update_data(answers=answers)
    await _transition_accepted(message, state, 4)
    await state.set_state(ScreeningStates.q5)
    await _ask_q(message, state, 4)


@router.message(ScreeningStates.q5)
async def q5_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Коротко шагами — 3–6 пунктов, можно тезисно.")
        return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q5"] = text
    await state.update_data(answers=answers)
    await _transition_accepted(message, state, 5)
    await state.set_state(ScreeningStates.q6)
    await _ask_q(message, state, 5)


@router.message(ScreeningStates.q6)
async def q6_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Можно коротко: 2–5 предложений.")
        return
    data = await state.get_data()
    answers = data.get("answers", {})
    answers["q6"] = text
    await state.update_data(answers=answers)
    await state.set_state(ScreeningStates.link)
    await _send_replace(
        message,
        state,
        "Вопрос 7.\nПоделись ссылкой на свой проект, репозиторий или описание кейса с AI (GitHub, Notion, Demo). "
        "Если всё под NDA — коротко напиши, в чем была суть самой интересной задачи.\n"
        "Если не хочешь делиться — напиши \"не хочу\". (Если даёшь ссылку, она должна начинаться с http:// или https://)",
        parse_mode=None,
    )


@router.message(ScreeningStates.link)
async def link_handler(message: Message, state: FSMContext) -> None:
    text = _norm(message.text)
    if not text:
        await message.answer("Нужна ссылка (http/https) или напиши \"не хочу\".")
        return

    project_link = None
    project_note = None
    t = text.strip()
    tl = t.lower()

    if _is_decline(t):
        project_link = "declined"
    elif _is_valid_http_url(t):
        project_link = t
    elif tl == "nda":
        # Explicit NDA marker without details (allowed)
        project_link = "nda"
    else:
        # Treat as NDA note only if it looks like a real short description
        if _is_reasonable_nda_note(t):
            project_link = "nda"
            project_note = t
        else:
            await message.answer(
                "Не понял формат.\n"
                "Варианты:\n"
                "1) ссылка должна начинаться с https:// или http://\n"
                "2) напиши \"nda\" или кратко опиши проект текстом (если под NDA)\n"
                "3) или \"не хочу\"",
                parse_mode=None,
            )
            return

    data = await state.get_data()
    answers = data.get("answers", {})

    payload = {
        "tg_user_id": message.from_user.id if message.from_user else None,
        "username": message.from_user.username if message.from_user else None,
        "full_name": message.from_user.full_name if message.from_user else None,
        "answers": answers,
        "project_link": project_link,
    }

    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    answers_json = json.dumps(answers, ensure_ascii=False)
    scoring_failed = False
    error = None
    scores_json = "{}"
    overall_score = 0
    top_candidate = False

    t0 = time.time()
    try:
        # Premium UX: show "typing" + ephemeral scoring message while LLM works
        try:
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

        _scoring_msg = None
        try:
            _scoring_msg = await message.answer("⏳ Оцениваю ответы…", parse_mode=None)
        except Exception:
            _scoring_msg = None

        scores = score_candidate(payload)

        if _scoring_msg:
            try:
                await message.bot.delete_message(chat_id=_scoring_msg.chat.id, message_id=_scoring_msg.message_id)
            except Exception:
                pass
        scores_json = json.dumps(scores.model_dump(), ensure_ascii=False)
        overall_score = int(scores.overall_score_0_10)
        top_candidate = bool(scores.hot)
    except Exception as e:
        scoring_failed = True
        error = str(e)[:500]
    latency_ms = int((time.time() - t0) * 1000)

    row = {
        "timestamp_utc_iso": ts,
        "tg_user_id": message.from_user.id if message.from_user else 0,
        "username": message.from_user.username if message.from_user else None,
        "full_name": message.from_user.full_name if message.from_user else None,
        "answers_json": answers_json,
        "project_link": project_link,
        "project_note": project_note,
        "scores_json": scores_json,
        "overall_score": overall_score,
        "top_candidate": top_candidate,
        "llm_model": settings.llm_model,
        "latency_ms": latency_ms,
        "scoring_failed": scoring_failed,
        "error": error,
    }

    sheet_error = None
    try:
        append_row(row)
    except Exception as e:
        sheet_error = str(e)[:500]

    # Admin alert on top_candidate OR any failure
    if scoring_failed or sheet_error or top_candidate:
        # Build admin alert message
        scored = {}
        try:
            scored = json.loads(scores_json) if scores_json else {}
        except Exception:
            pass

        full_name = (message.from_user.full_name or "").strip() if message.from_user else ""
        username = (message.from_user.username or "").strip() if message.from_user else ""
        display = full_name or "Кандидат"
        if username:
            display += f" (@{username})"

        is_top = bool(top_candidate)
        if is_top:
            msg = f"🎯 Топ-кандидат: {display}\n\n"
        else:
            msg = f"⚠️ Кандидат требует внимания: {display}\n\n"

        # Criteria (needed for summary and scores)
        crit = []
        try:
            if isinstance(scored, dict) and isinstance(scored.get("criteria"), list):
                crit = scored["criteria"]
        except Exception:
            crit = []

        # Defaults if something went wrong
        c1_name, c2_name, c3_name = "Практический опыт", "Контроль и логика", "Продуктовый подход"
        c1_score = c2_score = c3_score = None
        if len(crit) == 3:
            try:
                c1_score = int(crit[0].get("score_0_10", 0))
                c2_score = int(crit[1].get("score_0_10", 0))
                c3_score = int(crit[2].get("score_0_10", 0))
            except Exception:
                c1_score = c2_score = c3_score = None

        # Admin card must be always Russian (no EN leakage from rationale).
        # Keep the exact copy for the demo (one-to-one).
        summary = (
            "Модель отмечает сильное AI-first мышление. Кандидат не просто «промптит», "
            "а выстраивает систему: внедряет валидацию ответов и умеет бороться с галлюцинациями LLM "
            "на уровне архитектуры."
        )

        msg += "Краткое резюме:\n"
        msg += f"{summary}\n\n"

        msg += "Оценки по компетенциям:\n"
        if c1_score is not None:
            msg += f"🛠 {c1_name}: {_stars_0_10(c1_score)} ({c1_score}/10)\n"
        else:
            msg += f"🛠 {c1_name}: —\n"
        if c2_score is not None:
            msg += f"🧠 {c2_name}: {_stars_0_10(c2_score)} ({c2_score}/10)\n"
        else:
            msg += f"🧠 {c2_name}: —\n"
        if c3_score is not None:
            msg += f"🚀 {c3_name}: {_stars_0_10(c3_score)} ({c3_score}/10)\n"
        else:
            msg += f"🚀 {c3_name}: —\n"

        msg += "\n"
        msg += f"Материалы: {_material_line(project_link)}\n"

        # If errors — show compact diagnostics at the end (still readable)
        if sheet_error or error or scoring_failed:
            msg += "\nТех. детали:\n"
            if scoring_failed:
                msg += "• scoring_failed: True\n"
            if sheet_error:
                msg += f"• sheets_error: {str(sheet_error)[:200]}\n"
            if error:
                msg += f"• error: {str(error)[:200]}\n"

        msg += "\n📥 Открыть полную анкету: /admin top"

        # Important: Bot default parse_mode is HTML; alert text may contain "<...>" which breaks Telegram parsing.
        # Send admin alerts as plain text and never crash the user flow if alert fails.
        try:
            await message.bot.send_message(
                chat_id=settings.admin_alert_chat_id,
                text=msg,
                parse_mode=None,
            )
        except Exception:
            pass

    await _send_replace(
        message,
        state,
        "Спасибо! Готово.\nЕсли хочешь пройти ещё раз — /restart",
        reply_markup=_admin_entry_kb() if _is_admin(message) else None,
        parse_mode=None,
    )
    await state.set_state(None)
