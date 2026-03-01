import os
import sqlite3
import random
import string
import asyncio
import aiohttp
import time
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ---------- Configuration ----------
TELEGRAM_TOKEN = "8585265829:AAGyQ0V6_RivO7O5zzUXvoz5sscWErkf4qQ"          # Replace with your bot token
ADMIN_IDS = [8286198145]         # List of admin Telegram user IDs
MAIL_API_BASE = "https://api.mail.tm"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

BROADCAST_MESSAGE = 1

# ---------- Database Setup (No Ban Column) ----------
def init_db():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    # Users table without banned column
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            total_emails INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Check for missing columns
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if 'total_emails' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN total_emails INTEGER DEFAULT 0")

    # Accounts table – drop and recreate
    c.execute("DROP TABLE IF EXISTS accounts")
    c.execute("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT,
            password TEXT,
            token TEXT,
            account_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id)")

    conn.commit()
    conn.close()

# ---------- Database Functions ----------
def get_user(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def add_user(user_id, username, first_name):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)
    """, (user_id, username, first_name))
    conn.commit()
    conn.close()

def increment_email_count(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("UPDATE users SET total_emails = total_emails + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, total_emails FROM users ORDER BY created_at DESC")
    users = c.fetchall()
    conn.close()
    return users

def save_account(user_id, email, password, token, account_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO accounts (user_id, email, password, token, account_id)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, email, password, token, account_id))
    conn.commit()
    conn.close()

def get_user_accounts(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT id, email, password, token, account_id FROM accounts WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    accounts = c.fetchall()
    conn.close()
    return accounts

def get_account_by_id(account_row_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT user_id, email, password, token, account_id FROM accounts WHERE id = ?", (account_row_id,))
    account = c.fetchone()
    conn.close()
    return account

def delete_account_by_id(account_row_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("DELETE FROM accounts WHERE id = ?", (account_row_id,))
    conn.commit()
    conn.close()

# ---------- Async mail.tm API Helpers with Response Validation ----------
async def api_request(session, method, url, **kwargs):
    """Make an HTTP request with retry logic using aiohttp."""
    for attempt in range(MAX_RETRIES):
        try:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status == 429:
                    wait = RETRY_DELAY * (2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                # Always try to parse JSON; if fails, return text (unlikely for mail.tm)
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError:
                    # Not JSON – return text (caller must handle)
                    return await resp.text()
        except aiohttp.ClientResponseError as e:
            if e.status == 429:
                wait = RETRY_DELAY * (2 ** attempt)
                await asyncio.sleep(wait)
                continue
            raise
        except aiohttp.ClientError as e:
            if attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(RETRY_DELAY * (2 ** attempt))
    raise Exception("Max retries exceeded for API request")

async def get_domains(session):
    data = await api_request(session, "GET", f"{MAIL_API_BASE}/domains")
    if not isinstance(data, dict):
        raise Exception(f"Invalid response from domains API: {data}")
    domains = data.get('hydra:member', [])
    if not domains:
        raise Exception("No domains available")
    return domains[0]['domain']

async def create_account(session, email, password):
    payload = {"address": email, "password": password}
    data = await api_request(session, "POST", f"{MAIL_API_BASE}/accounts", json=payload)
    if not isinstance(data, dict):
        raise Exception(f"Invalid response from create_account API: {data}")
    return data

async def get_token(session, email, password):
    payload = {"address": email, "password": password}
    data = await api_request(session, "POST", f"{MAIL_API_BASE}/token", json=payload)
    if not isinstance(data, dict) or "token" not in data:
        raise Exception(f"Invalid token response: {data}")
    return data["token"]

async def get_messages(session, token):
    headers = {"Authorization": f"Bearer {token}"}
    data = await api_request(session, "GET", f"{MAIL_API_BASE}/messages", headers=headers)
    if not isinstance(data, dict):
        # Unexpected response – treat as empty
        return []
    return data.get("hydra:member", [])

async def get_message(session, token, msg_id):
    headers = {"Authorization": f"Bearer {token}"}
    data = await api_request(session, "GET", f"{MAIL_API_BASE}/messages/{msg_id}", headers=headers)
    if not isinstance(data, dict):
        raise Exception(f"Invalid message response: {data}")
    return data

async def delete_account_api(session, account_id, token):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        await api_request(session, "DELETE", f"{MAIL_API_BASE}/accounts/{account_id}", headers=headers)
        return True
    except:
        return False

# ---------- Utility ----------
def generate_random_localpart(length=10):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def format_message(msg):
    """Return a clean monospace formatted message string."""
    subject = msg.get("subject", "(no subject)")
    from_ = msg.get("from", {}).get("address", "unknown")
    date = msg.get("createdAt", "")

    # Prefer text/plain over HTML
    text_part = next((p for p in msg.get("text", []) if p), None)
    html_part = next((p for p in msg.get("html", []) if p), None)

    if text_part:
        content = text_part
    elif html_part:
        # Remove HTML tags and also strip style/script blocks
        content = re.sub(r'<style.*?>.*?</style>', '', html_part, flags=re.DOTALL)
        content = re.sub(r'<script.*?>.*?</script>', '', content, flags=re.DOTALL)
        content = re.sub(r'<[^>]+>', '', content)  # remove remaining tags
        content = re.sub(r'\n\s*\n', '\n\n', content)  # collapse multiple blank lines
    else:
        content = "(No content)"

    # Trim if too long
    if len(content) > 3000:
        content = content[:3000] + "… (truncated)"

    return f"```\nSubject: {subject}\nFrom: {from_}\nDate: {date}\n\n{content}\n```"

# ---------- Bot Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)

    keyboard = [
        [KeyboardButton("📧 New Email"), KeyboardButton("📥 Inbox")],
        [KeyboardButton("❌ Delete Account"), KeyboardButton("ℹ️ Help")]
    ]
    if user.id in ADMIN_IDS:
        keyboard.append([KeyboardButton("👑 Admin Panel")])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "👋 Welcome to TempMail Bot!\n\nChoose an option:",
        reply_markup=reply_markup
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if text == "📧 New Email":
        await new_email(update, context)
    elif text == "📥 Inbox":
        await inbox(update, context)
    elif text == "❌ Delete Account":
        await delete_email(update, context)
    elif text == "ℹ️ Help":
        await help_command(update, context)
    elif text == "👑 Admin Panel" and user.id in ADMIN_IDS:
        await admin_panel(update, context)
    else:
        await update.message.reply_text("Unknown command. Use the menu buttons.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "📧 New Email – Generate a new temporary email\n"
        "📥 Inbox – Show received messages\n"
        "❌ Delete Account – Delete an email account\n"
        "ℹ️ Help – Show this help"
    )

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = await update.message.reply_text("⏳ Creating your temporary email...")
    try:
        async with aiohttp.ClientSession() as session:
            domain = await get_domains(session)
            local = generate_random_localpart()
            email = f"{local}@{domain}"
            password = generate_random_localpart(12)

            account_info = await create_account(session, email, password)
            token = await get_token(session, email, password)

        save_account(user.id, email, password, token, account_info["id"])
        increment_email_count(user.id)

        await msg.edit_text(
            f"✅ New temporary email created:\n`{email}`\n\nUse 📥 Inbox to check messages.",
            parse_mode="Markdown"
        )
    except Exception as e:
        error = str(e)
        if "429" in error:
            await msg.edit_text("❌ Too many requests. Please wait a minute and try again.")
        else:
            await msg.edit_text(f"❌ Error: {error}")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    accounts = get_user_accounts(user.id)
    if not accounts:
        await update.message.reply_text("You don't have any email yet. Use 📧 New Email to create one.")
        return

    if len(accounts) == 1:
        await show_inbox_for_account(update, context, accounts[0][0])
    else:
        keyboard = []
        for acc in accounts:
            acc_id, email, _, _, _ = acc
            display = email if len(email) < 30 else email[:27] + "..."
            keyboard.append([InlineKeyboardButton(display, callback_data=f"inbox_acc_{acc_id}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Select an email account to check:", reply_markup=reply_markup)

async def show_inbox_for_account(update: Update, context: ContextTypes.DEFAULT_TYPE, acc_id, edit=False):
    account = get_account_by_id(acc_id)
    if not account:
        text = "Account not found."
        if edit:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    user_id, email, password, token, account_id = account
    try:
        async with aiohttp.ClientSession() as session:
            try:
                msgs = await get_messages(session, token)
            except aiohttp.ClientResponseError as e:
                if e.status == 401:
                    new_token = await get_token(session, email, password)
                    # Update token in DB
                    conn = sqlite3.connect("bot_data.db")
                    c = conn.cursor()
                    c.execute("UPDATE accounts SET token = ? WHERE id = ?", (new_token, acc_id))
                    conn.commit()
                    conn.close()
                    msgs = await get_messages(session, new_token)
                else:
                    raise

        if not msgs:
            text = f"📭 No messages for {email}."
            reply_markup = None
        else:
            keyboard = []
            for msg in msgs:
                msg_id = msg["id"]
                subject = msg.get("subject", "(no subject)")[:30]
                keyboard.append([InlineKeyboardButton(subject, callback_data=f"read_{acc_id}_{msg_id}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            text = f"📬 Messages for {email}:"

        if edit:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)
    except Exception as e:
        err = f"❌ Error: {e}"
        if edit:
            await update.callback_query.edit_message_text(err)
        else:
            await update.message.reply_text(err)

async def inbox_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split("_")[2])
    await show_inbox_for_account(update, context, acc_id, edit=True)

async def read_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")
    acc_id = int(parts[1])
    msg_id = parts[2]

    account = get_account_by_id(acc_id)
    if not account:
        await query.edit_message_text("Account not found.")
        return

    user_id, email, password, token, account_id = account
    try:
        async with aiohttp.ClientSession() as session:
            try:
                msg = await get_message(session, token, msg_id)
            except aiohttp.ClientResponseError as e:
                if e.status == 401:
                    new_token = await get_token(session, email, password)
                    # Update token in DB
                    conn = sqlite3.connect("bot_data.db")
                    c = conn.cursor()
                    c.execute("UPDATE accounts SET token = ? WHERE id = ?", (new_token, acc_id))
                    conn.commit()
                    conn.close()
                    msg = await get_message(session, new_token, msg_id)
                else:
                    raise

        formatted = format_message(msg)
        await query.edit_message_text(formatted, parse_mode="MarkdownV2")
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {e}")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    accounts = get_user_accounts(user.id)
    if not accounts:
        await update.message.reply_text("No email accounts to delete.")
        return

    keyboard = []
    for acc in accounts:
        acc_id, email, _, _, _ = acc
        display = email if len(email) < 30 else email[:27] + "..."
        keyboard.append([InlineKeyboardButton(f"❌ {display}", callback_data=f"del_acc_{acc_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select an account to delete:", reply_markup=reply_markup)

async def delete_account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    acc_id = int(query.data.split("_")[2])
    account = get_account_by_id(acc_id)
    if not account:
        await query.edit_message_text("Account not found.")
        return

    user_id, email, password, token, account_id = account
    try:
        async with aiohttp.ClientSession() as session:
            await delete_account_api(session, account_id, token)
    except:
        pass
    delete_account_by_id(acc_id)
    await query.edit_message_text(f"✅ Account `{email}` deleted.", parse_mode="Markdown")

# ---------- Admin Handlers ----------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📋 User List", callback_data="admin_userlist")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="admin_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👑 Admin Panel", reply_markup=reply_markup)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if user.id not in ADMIN_IDS:
        await query.edit_message_text("Access denied.")
        return

    data = query.data
    if data == "admin_userlist":
        users = get_all_users()
        if not users:
            await query.edit_message_text("No users yet.")
            return
        keyboard = []
        for uid, username, first_name, total_emails in users:
            display_name = username or first_name or str(uid)
            label = f"{display_name} ({total_emails})"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"user_{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_back")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Select a user:", reply_markup=reply_markup)

    elif data.startswith("user_"):
        uid = int(data.split("_")[1])
        users = get_all_users()
        target = next((u for u in users if u[0] == uid), None)
        if not target:
            await query.edit_message_text("User not found.")
            return
        uid, username, first_name, total_emails = target
        display_name = username or first_name or str(uid)
        text = f"User: {display_name}\nID: {uid}\nEmails created: {total_emails}"
        keyboard = [[InlineKeyboardButton("🔙 Back to list", callback_data="admin_userlist")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)

    elif data == "admin_broadcast":
        context.user_data["broadcast"] = True
        await query.edit_message_text(
            "Send me the message to broadcast.\nType /cancel to abort."
        )
        return BROADCAST_MESSAGE

    elif data == "admin_back":
        keyboard = [
            [KeyboardButton("📧 New Email"), KeyboardButton("📥 Inbox")],
            [KeyboardButton("❌ Delete Account"), KeyboardButton("ℹ️ Help")],
        ]
        if user.id in ADMIN_IDS:
            keyboard.append([KeyboardButton("👑 Admin Panel")])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.message.delete()
        await context.bot.send_message(
            chat_id=user.id,
            text="Main menu:",
            reply_markup=reply_markup
        )

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["broadcast"] = True
    await update.message.reply_text(
        "Send me the message to broadcast.\nType /cancel to abort."
    )
    return BROADCAST_MESSAGE

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    text = update.message.text
    users = get_all_users()
    success = 0
    failed = 0
    for uid, _, _, _ in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            success += 1
        except:
            failed += 1
    await update.message.reply_text(f"Broadcast complete.\nSent: {success}\nFailed: {failed}")
    context.user_data["broadcast"] = False
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS and context.user_data.get("broadcast"):
        context.user_data["broadcast"] = False
        await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END

# ---------- Main ----------
def main():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    broadcast_conv = ConversationHandler(
        entry_points=[
            CommandHandler("broadcast", broadcast_start),
            CallbackQueryHandler(admin_callback, pattern="^admin_broadcast$")
        ],
        states={
            BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_message)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    application.add_handler(CallbackQueryHandler(read_message_callback, pattern="^read_\\d+_.+"))
    application.add_handler(CallbackQueryHandler(inbox_account_callback, pattern="^inbox_acc_\\d+$"))
    application.add_handler(CallbackQueryHandler(delete_account_callback, pattern="^del_acc_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^user_"))
    application.add_handler(broadcast_conv)

    print("Bot started with async HTTP and response validation. Press Ctrl+C to stop.")
    application.run_polling()

if __name__ == "__main__":
    main()
