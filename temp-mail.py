import os
import sqlite3
import random
import string
import requests
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
TELEGRAM_TOKEN = "8561624536:AAHQQb_Q5BtvvrNlA8mZC0668CUxHvR9yYk"          # Replace with your bot token
ADMIN_IDS = [8286198145]         # List of admin Telegram user IDs
MAIL_API_BASE = "https://api.mail.tm"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

BROADCAST_MESSAGE = 1

# ---------- Database Setup with Migration ----------
def init_db():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    # Users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            total_emails INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Check for missing columns in users
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if 'total_emails' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN total_emails INTEGER DEFAULT 0")
    if 'banned' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")

    # Accounts table – drop and recreate to allow multiple per user
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

def ban_user(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect("bot_data.db")
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, total_emails, banned FROM users ORDER BY created_at DESC")
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
    return accounts  # list of tuples

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

# ---------- mail.tm API Helpers (with retry) ----------
def api_request(method, url, **kwargs):
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = RETRY_DELAY * (2 ** attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else None
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = RETRY_DELAY * (2 ** attempt)
                time.sleep(wait)
                continue
            raise
    raise Exception("Max retries exceeded for API request")

def get_domains():
    data = api_request("GET", f"{MAIL_API_BASE}/domains")
    domains = data['hydra:member']
    if not domains:
        raise Exception("No domains available")
    return domains[0]['domain']

def create_account(email, password):
    payload = {"address": email, "password": password}
    return api_request("POST", f"{MAIL_API_BASE}/accounts", json=payload)

def get_token(email, password):
    payload = {"address": email, "password": password}
    data = api_request("POST", f"{MAIL_API_BASE}/token", json=payload)
    return data["token"]

def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    data = api_request("GET", f"{MAIL_API_BASE}/messages", headers=headers)
    return data.get("hydra:member", [])

def get_message(token, msg_id):
    headers = {"Authorization": f"Bearer {token}"}
    return api_request("GET", f"{MAIL_API_BASE}/messages/{msg_id}", headers=headers)

def delete_account_api(account_id, token):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        api_request("DELETE", f"{MAIL_API_BASE}/accounts/{account_id}", headers=headers)
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

    # Trim if too long (Telegram limit ~4000, but we keep some margin)
    if len(content) > 3000:
        content = content[:3000] + "… (truncated)"

    # Return as monospace block
    return f"```\nSubject: {subject}\nFrom: {from_}\nDate: {date}\n\n{content}\n```"

# ---------- Bot Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)

    db_user = get_user(user.id)
    if db_user and db_user[4]:  # banned
        await update.message.reply_text("🚫 You are banned.")
        return

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

    db_user = get_user(user.id)
    if db_user and db_user[4]:
        await update.message.reply_text("🚫 You are banned.")
        return

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
        domain = get_domains()
        local = generate_random_localpart()
        email = f"{local}@{domain}"
        password = generate_random_localpart(12)

        account_info = create_account(email, password)
        token = get_token(email, password)

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
        # Directly show inbox for the only account
        await show_inbox_for_account(update, context, accounts[0][0])
    else:
        # Show account selection
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
        try:
            msgs = get_messages(token)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                new_token = get_token(email, password)
                # Update token in DB
                conn = sqlite3.connect("bot_data.db")
                c = conn.cursor()
                c.execute("UPDATE accounts SET token = ? WHERE id = ?", (new_token, acc_id))
                conn.commit()
                conn.close()
                msgs = get_messages(new_token)
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
        try:
            msg = get_message(token, msg_id)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                new_token = get_token(email, password)
                # Update token in DB
                conn = sqlite3.connect("bot_data.db")
                c = conn.cursor()
                c.execute("UPDATE accounts SET token = ? WHERE id = ?", (new_token, acc_id))
                conn.commit()
                conn.close()
                msg = get_message(new_token, msg_id)
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
        delete_account_api(account_id, token)
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
        for uid, username, first_name, total_emails, banned in users:
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
        uid, username, first_name, total_emails, banned = target
        display_name = username or first_name or str(uid)
        status = "🚫 Banned" if banned else "✅ Active"
        text = f"User: {display_name}\nID: {uid}\nEmails created: {total_emails}\nStatus: {status}"
        keyboard = []
        if banned:
            keyboard.append([InlineKeyboardButton("🔓 Unban", callback_data=f"unban_{uid}")])
        else:
            keyboard.append([InlineKeyboardButton("🔒 Ban", callback_data=f"ban_{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 Back to list", callback_data="admin_userlist")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup)

    elif data.startswith("ban_"):
        uid = int(data.split("_")[1])
        ban_user(uid)
        await query.answer("User banned.")
        query.data = "admin_userlist"
        await admin_callback(update, context)

    elif data.startswith("unban_"):
        uid = int(data.split("_")[1])
        unban_user(uid)
        await query.answer("User unbanned.")
        query.data = "admin_userlist"
        await admin_callback(update, context)

    elif data == "admin_broadcast":
        context.user_data["broadcast"] = True
        await query.edit_message_text(
            "Send me the message to broadcast.\nType /cancel to abort."
        )
        return BROADCAST_MESSAGE

    elif data == "admin_back":
        # Return to main menu with reply keyboard
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
    for uid, _, _, _, banned in users:
        if banned:
            continue
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

    # Conversation handler for broadcast
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

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    application.add_handler(CallbackQueryHandler(read_message_callback, pattern="^read_\\d+_.+"))
    application.add_handler(CallbackQueryHandler(inbox_account_callback, pattern="^inbox_acc_\\d+$"))
    application.add_handler(CallbackQueryHandler(delete_account_callback, pattern="^del_acc_\\d+$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^user_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^ban_"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^unban_"))
    application.add_handler(broadcast_conv)

    print("Bot started. Press Ctrl+C to stop.")
    application.run_polling()

if __name__ == "__main__":
    main()