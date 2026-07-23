import asyncio
import time
import uuid
import logging
import random
import os
from aiohttp import web
from telethon import TelegramClient, events, types
from telethon.tl.types import InputBotInlineResult, InputBotInlineMessageText
from telethon.errors.rpcerrorlist import MessageNotModifiedError

from tes.question import questions

async def health_check(request):
    logger.info("Health check endpoint was called.")
    return web.Response(text="Bot is running and healthy!")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

API_ID = os.environ.get('API_ID', '3335796')
API_HASH = os.environ.get('API_HASH', '138b992a0e672e8346d8439c3f42ea78')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8189638115:AAHJ-gXcWm6i25nSmpcr7QnTPAhf9nuOwF4')

app = TelegramClient("watermark_bot", api_id=API_ID, api_hash=API_HASH)

game_sessions = {}
active_timeouts = {}

import telethon
logger.info(f"Telethon version: {telethon.__version__}")


def cancel_timeout_task(session_key):
    """
    حذف/کنسل کردن تسک تایم‌اوت مربوط به یک سشن.

    نکته‌ی مهم (فیکس باگ): وقتی این تابع از *داخل خودِ* تسک question_timeout
    (چه مستقیم، چه از طریق ask_question_in_chat که همون تسک صداش می‌زنه)
    فراخوانی بشه، تسکی که می‌خوایم کنسل کنیم همون تسکِ در حال اجراست.
    اگر در این حالت واقعاً .cancel() صدا زده بشه، در اولین await بعدی
    یک CancelledError پرتاب می‌شه و تسک بدون رسیدن به کد ادامه (رفتن به
    سوال بعدی) متوقف می‌شه. برای همین در این حالت فقط از دیکشنری پاک
    می‌کنیم و کاری به cancel واقعی نداریم.
    """
    if session_key in active_timeouts:
        task = active_timeouts.pop(session_key)
        if task is asyncio.current_task():
            logger.info(
                f"TASK_CANCEL: Session {session_key} timeout entry removed "
                f"(self-reference from within its own task, not cancelling)."
            )
            return True
        task.cancel()
        logger.info(f"TASK_CANCEL: Timeout task for session {session_key} was cancelled and removed.")
        return True
    return False


async def cleanup_old_sessions():
    try:
        while True:
            await asyncio.sleep(600)
            expired_keys = [key for key, session in game_sessions.items() if time.time() - session.get("created_at", 0) > 1200]
            for key in expired_keys:
                logger.info(f"CLEANUP: Cleaning up expired session {key}")
                cancel_timeout_task(key)
                if key in game_sessions:
                    del game_sessions[key]
    except asyncio.CancelledError:
        logger.info("Cleanup task cancelled")
        raise

def get_players_text(session):
    if not session["players"]:
        return "🧑‍🤝‍🧑 لیست پایه‌ها:\n(هنوز کسی پایه نیست)"
    text = "🧑‍🤝‍🧑 لیست پایه‌ها:\n"
    sorted_players = sorted(session["players"], key=lambda p: p['score'], reverse=True)
    player_lines = []
    for p in sorted_players:
        score_text = f" | امتیاز: {p['score']}" if session['started'] else ""
        player_lines.append(f"👤 {p['name']}{score_text}")
    text += "\n".join(player_lines)
    return text

def get_initial_markup(session_key):
    session = game_sessions[session_key]
    rows = [
        types.KeyboardButtonRow([types.KeyboardButtonCallback("🙋‍♂️ من پایه‌ام", data=f"im_in|{session_key}".encode())]),
        types.KeyboardButtonRow([types.KeyboardButtonCallback("🚀 شروع بازی", data=f"start_game|{session_key}".encode())])
    ]
    if session["players"]:
        rows.append(types.KeyboardButtonRow([types.KeyboardButtonCallback("❌ لغو بازی", data=f"cancel_game|{session_key}".encode())]))
    rows.append(types.KeyboardButtonRow([types.KeyboardButtonSwitchInline("👥 دعوت دوستان", query="")]))
    return types.ReplyInlineMarkup(rows)

async def edit_game_message(client, session, text, buttons):
    try:
        if session["is_inline_message"]:
            if "event" in session and session["event"]:
                await session["event"].edit(text=text, buttons=buttons)
            else:
                logger.error(f"EDIT_HELPER: Cannot edit inline message for session {session['session_key']} because event object is missing.")
                return
        else:
            await client.edit_message(entity=session["main_chat_id"], message=session["main_message_id"], text=text, buttons=buttons)
        logger.info(f"EDIT_HELPER: Message updated for session {session['session_key']}")
    except MessageNotModifiedError:
        logger.warning(f"EDIT_HELPER: Message not modified for session {session['session_key']}. Skipping.")
    except Exception as e:
        logger.error(f"EDIT_HELPER: Failed to edit message for session {session['session_key']}: {e}", exc_info=True)

@app.on(events.NewMessage(pattern='/start', incoming=True))
async def start_command_handler(event):
    if not event.is_private:
        await event.respond("برای شروع بازی در گروه، لطفاً نام من را (`@YourBotUsername`) تایپ کرده و گزینه 'ایجاد چالش' را انتخاب کنید.", reply_to=event.message)
        return
    chat_id = event.chat_id
    session_key = str(chat_id)
    cancel_timeout_task(session_key)
    session = {
        "session_key": session_key, "players": [], "started": False, "starter_id": event.sender_id,
        "questions": random.sample(questions, min(10, len(questions))), "is_inline_message": False,
        "main_message_id": None, "main_chat_id": chat_id, "current_q_index": 0,
        "created_at": time.time(), "responded_users": set(), "event": None,
        "active_question": False
    }
    game_sessions[session_key] = session
    logger.info(f"SESSION_CREATE: Private session '{session_key}' created for user {event.sender_id}.")
    text = "🎉 به چالش اطلاعات خوش آمدید!\nبرای شرکت در بازی روی دکمه 'من پایه‌ام' کلیک کنید.\n\n" + get_players_text(session)
    markup = get_initial_markup(session_key)
    sent_message = await event.respond(text, buttons=markup)
    session["main_message_id"] = sent_message.id

@app.on(events.InlineQuery())
async def handle_inline_query(event):
    session_key = str(uuid.uuid4())
    session = {
        "session_key": session_key, "players": [], "started": False, "starter_id": event.sender_id,
        "questions": random.sample(questions, min(10, len(questions))), "is_inline_message": True,
        "main_message_id": None, "main_chat_id": None, "current_q_index": 0,
        "created_at": time.time(), "responded_users": set(), "event": None,
        "active_question": False
    }
    game_sessions[session_key] = session
    logger.info(f"SESSION_CREATE: Inline session '{session_key}' created for user {event.sender_id}.")
    initial_text = "🎉 به چالش اطلاعات خوش آمدید!\nبرای شرکت در بازی روی دکمه 'من پایه‌ام' کلیک کنید.\n\n" + get_players_text(session)
    markup = get_initial_markup(session_key)
    result = InputBotInlineResult(id=str(uuid.uuid4()), type='article', title="ایجاد چالش اطلاعات!",
                                  description="دوستان خود را به یک مسابقه هیجان‌انگیز دعوت کنید!",
                                  send_message=InputBotInlineMessageText(message=initial_text, reply_markup=markup))
    await event.answer([result], cache_time=1)

@app.on(events.CallbackQuery())
async def handle_buttons(event):
    data_parts = event.data.decode('utf-8').split('|')
    action = data_parts[0]
    session_key = data_parts[1] if len(data_parts) > 1 else None
    if not session_key or session_key not in game_sessions:
        await event.answer("این بازی منقضی شده یا دیگر وجود ندارد. لطفاً یک بازی جدید شروع کنید.", alert=True)
        return
    session = game_sessions[session_key]
    if session["is_inline_message"] and not session.get("event"):
        session["event"] = event
        logger.info(f"EVENT_CAPTURE: Captured event object for inline session {session_key}")

    if action == "im_in":
        if session["started"]:
            await event.answer("🚫 بازی شروع شده و دیگر نمی‌توانید به آن ملحق شوید!", alert=True)
            return
        user_id = event.sender_id
        if user_id not in [p["id"] for p in session["players"]]:
            user = await event.get_sender()
            player_name = user.first_name or user.username or f"User_{user_id}"
            session["players"].append({"id": user_id, "name": player_name, "score": 0})
            await event.answer("✅ شما به لیست پایه‌ها اضافه شدید!", alert=False)
            text_to_update = "🎉 به چالش اطلاعات خوش آمدید!\nبرای شرکت در بازی روی دکمه 'من پایه‌ام' کلیک کنید.\n\n" + get_players_text(session)
            markup = get_initial_markup(session_key)
            await edit_game_message(app, session, text_to_update, markup)
        else:
            await event.answer("شما از قبل در لیست هستید!", alert=False)

    elif action == "start_game":
        if event.sender_id != session.get("starter_id"):
            await event.answer("فقط شروع‌کننده می‌تواند بازی را استارت بزند!", alert=True)
            return
        if not session["players"]:
            await event.answer("هنوز هیچکس پایه نیست! نمی‌توان بازی را شروع کرد.", alert=True)
            return
        if session["started"]:
            await event.answer("بازی قبلاً شروع شده است!", alert=True)
            return
        await event.answer("🚀 بازی شروع می‌شود!")
        session["started"] = True
        await ask_question_in_chat(app, session_key)

    elif action == "cancel_game":
        if event.sender_id != session.get("starter_id"):
            await event.answer("فقط شروع‌کننده می‌تواند بازی را لغو کند!", alert=True)
            return
        text_to_update = "❌ بازی توسط شروع‌کننده لغو شد."
        await edit_game_message(app, session, text_to_update, None)
        cancel_timeout_task(session_key)
        if session_key in game_sessions:
            del game_sessions[session_key]
        logger.info(f"SESSION_CANCEL: Session {session_key} was cancelled by starter.")

    elif action == "answer":
        # data_parts[2] اکنون به‌جای متنِ کامل گزینه، ایندکس گزینه است
        # (فیکس ButtonDataInvalidError برای گزینه‌های فارسی طولانی)
        await handle_answer(app, event, session_key, data_parts[2])

def calculate_score(elapsed):
    return max(0, 20 - (int(elapsed) * 2))

async def handle_answer(client, event, session_key, selected_option_raw):
    session = game_sessions.get(session_key)
    if not session:
        return
    user_id = event.sender_id
    player = next((p for p in session["players"] if p["id"] == user_id), None)
    if not player:
        await event.answer("شما در این بازی شرکت نکرده‌اید!", alert=True)
        return
    if not session.get("active_question"):
        await event.answer("زمان پاسخ به این سوال تمام شده است!", alert=True)
        return
    if user_id in session["responded_users"]:
        await event.answer("شما قبلاً به این سوال پاسخ داده‌اید!", alert=True)
        return

    # ایندکس گزینه انتخاب‌شده را به متن واقعی گزینه تبدیل می‌کنیم
    try:
        selected_index = int(selected_option_raw)
        selected_option = session["current_question_options"][selected_index]
    except (ValueError, IndexError):
        logger.error(f"ANSWER: Invalid option index '{selected_option_raw}' for session {session_key}")
        await event.answer("خطایی رخ داد، دوباره تلاش کنید.", alert=True)
        return

    session["responded_users"].add(user_id)
    q = session["questions"][session["current_q_index"]]
    correct_answer = q["answer"]
    elapsed = time.time() - session["question_start_time"]
    response_text = "❌ اشتباه بود!"
    if selected_option == correct_answer:
        earned_score = calculate_score(elapsed)
        player["score"] += earned_score
        response_text = f"✅ پاسخ صحیح! | {earned_score}+ امتیاز"
    await event.answer(response_text, alert=False)
    logger.info(f"ANSWER: User {user_id} in session {session_key} answered. Correct: {selected_option == correct_answer}. New score: {player['score']}")


async def ask_question_in_chat(client, session_key):
    session = game_sessions.get(session_key)
    if not session:
        logger.warning(f"ASK_QUESTION: Session {session_key} not found. Aborting.")
        return
    if session["current_q_index"] >= len(session["questions"]):
        await announce_final_results(client, session_key)
        return

    cancel_timeout_task(session_key)

    session["responded_users"].clear()
    session["active_question"] = True
    session["question_start_time"] = time.time()

    q = session["questions"][session["current_q_index"]]
    options_list = q["options"][:]
    random.shuffle(options_list)
    session["current_question_options"] = options_list

    # به‌جای فرستادن متنِ کامل گزینه در callback_data، فقط ایندکس آن فرستاده می‌شود.
    # این کار طول callback_data را همیشه کوچک و ثابت نگه می‌دارد (زیر محدودیت ۶۴ بایتی
    # تلگرام) صرف‌نظر از طول متن گزینه‌ها یا تعداد بازیکنان.
    buttons = [
        types.KeyboardButtonCallback(text=opt, data=f"answer|{session_key}|{i}".encode())
        for i, opt in enumerate(options_list)
    ]
    rows = [types.KeyboardButtonRow(buttons[i:i+2]) for i in range(0, len(buttons), 2)]
    markup = types.ReplyInlineMarkup(rows)
    question_text = (
        f"سوال {session['current_q_index'] + 1} از {len(session['questions'])}\n\n"
        f"❓ **{q['question']}**\n\n"
        f"۱۰ ثانیه فرصت پاسخگویی دارید..."
    )
    full_text = get_players_text(session) + "\n\n" + question_text
    await edit_game_message(client, session, full_text, markup)

    timeout_task = asyncio.create_task(question_timeout(client, session_key))
    active_timeouts[session_key] = timeout_task
    logger.info(f"ASK_QUESTION: Question {session['current_q_index'] + 1} sent. New timeout task created for session {session_key}.")

async def question_timeout(client, session_key):
    try:
        await asyncio.sleep(10)
        session = game_sessions.get(session_key)
        if not session or not session.get("active_question"):
            logger.warning(f"TIMEOUT: Timeout aborted for session {session_key}. Session not found or question is inactive.")
            return
        logger.info(f"TIMEOUT: Processing timeout for session {session_key}, question {session['current_q_index'] + 1}.")
        session["active_question"] = False
        q = session["questions"][session["current_q_index"]]
        correct_answer = q["answer"]
        players_summary_text = get_players_text(session)
        timeout_text = (
            f"{players_summary_text}\n\n"
            f"⏰ زمان پاسخ تمام شد!\n"
            f"جواب صحیح: **{correct_answer}**\n\n"
            f"آماده برای سوال بعدی..."
        )
        await edit_game_message(client, session, timeout_text, None)

        # current_q_index را زیاد کن
        session["current_q_index"] += 1

        # پاک کردن رفرنس تایمر قبلی (بدون self-cancel، چون خودِ همین تسکه)
        cancel_timeout_task(session_key)

        await asyncio.sleep(3)
        # اگر سوالات تمام شده بود، نتایج را اعلام کن
        if session["current_q_index"] >= len(session["questions"]):
            await announce_final_results(client, session_key)
        else:
            await ask_question_in_chat(client, session_key)
    except asyncio.CancelledError:
        logger.info(f"TIMEOUT: Task for session {session_key} was cancelled as expected.")
    except Exception as e:
        logger.error(f"TIMEOUT_ERROR: Unexpected error for session {session_key}: {e}", exc_info=True)
        cancel_timeout_task(session_key)
        if session_key in game_sessions:
            del game_sessions[session_key]

async def announce_final_results(client, session_key):
    session = game_sessions.get(session_key)
    if not session: return
    sorted_players = sorted(session["players"], key=lambda p: p['score'], reverse=True)
    final_text = "🏆 نتایج نهایی چالش 🏆\n\n"
    if sorted_players:
        for i, p in enumerate(sorted_players):
            emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "▫️"
            final_text += f"{emoji} {p['name']}: {p['score']} امتیاز\n"
    else:
        final_text += "هیچ بازیکنی در این دور شرکت نکرد."
    final_text += "\nبازی تمام شد! برای شروع یک بازی جدید، از دکمه زیر استفاده کنید."
    invite_button = types.KeyboardButtonRow([types.KeyboardButtonSwitchInline("👥 شروع یک بازی جدید", query="")])
    final_markup = types.ReplyInlineMarkup([invite_button])
    await edit_game_message(client, session, final_text, final_markup)
    cancel_timeout_task(session_key)
    if session_key in game_sessions:
        del game_sessions[session_key]
    logger.info(f"SESSION_END: Final results announced and session {session_key} cleaned up.")

async def main():
    await app.start(bot_token=BOT_TOKEN)
    logger.info("Bot client started successfully.")
    cleanup_task = asyncio.create_task(cleanup_old_sessions())
    logger.info("Cleanup task scheduled.")
    webapp = web.Application()
    webapp.router.add_get('/', health_check)
    runner = web.AppRunner(webapp)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    logger.info(f"Starting web server on port {port} to handle health checks...")
    await site.start()
    logger.info("Bot is fully running and waiting for events. Web server is active.")
    await app.run_until_disconnected()
    logger.info("Bot disconnected. Cleaning up resources...")
    cleanup_task.cancel()
    await runner.cleanup()
    logger.info("Cleanup complete. Bot stopped.")

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user. Exiting.")
