import os
import json
import logging
import base64
import urllib.parse
from io import BytesIO

import httpx
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── Configuration ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
ROYALE_API_KEY    = os.getenv("ROYALE_API_KEY", "")
GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_KEY", "")
ROYALE_BASE       = "https://api.clashroyale.com/v1"
DATA_FILE         = "users.json"

CARD_W, CARD_H = 128, 128
GAP            = 8
BG_COLOR       = (20, 20, 32)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── Fonctions Utilitaires & API ──────────────────────────────────────────────
def load_users() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f: return json.load(f)
        except: return {}
    return {}

def save_users(data: dict):
    with open(DATA_FILE, "w") as f: json.dump(data, f, indent=2)

def hdrs():
    return {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{ROYALE_BASE}{path}", headers=hdrs())
        if r.status_code == 200: return r.json()
        return None

async def get_player(tag: str):
    clean_tag = tag.strip().upper().replace("#", "%23")
    return await api_get(f"/players/{clean_tag}")

async def get_battles(tag: str) -> list:
    clean_tag = tag.strip().upper().replace("#", "%23")
    d = await api_get(f"/players/{clean_tag}/battlelog")
    return d if isinstance(d, list) else []

async def search_players(name: str) -> list:
    safe_name = urllib.parse.quote(name)
    d = await api_get(f"/players?name={safe_name}&limit=10")
    return d.get("items", []) if d else []

# ─── Logique Stats & Deck ──────────────────────────────────────────────────────
def uc_info(player: dict) -> tuple:
    """Récupère le statut Champion Suprême et le Max ELO."""
    is_uc = False
    max_elo = 0
    for b in player.get("badges", []):
        if "ultimatechampion" in b.get("name", "").lower():
            is_uc = True
            max_elo = max(max_elo, b.get("progress", 0), b.get("value", 0))
    best = player.get("leagueStatistics", {}).get("bestSeason", {})
    if best:
        is_uc = True
        max_elo = max(max_elo, best.get("trophies", 0))
    return is_uc, max_elo

def get_deck_info(cards: list) -> str:
    """Affiche le coût, les évolutions et les héros (sans la liste des noms)."""
    if not cards: return "Deck inconnu"
    avg = sum(c.get("elixirCost", 0) for c in cards) / 8
    
    # Détection évolutions et héros
    evos = [c.get("name") for c in cards if "evolution" in c.get("iconUrls", {})]
    heroes_list = ["Little Prince", "Archer Queen", "Golden Knight", "Skeleton King", "Mighty Miner", "Monk"]
    found_heroes = [c.get("name") for c in cards if c.get("name") in heroes_list]
    
    text = f"⚡ Coût moyen : `{avg:.1f}`"
    if evos: text += f"\n🧬 Évolutions : _{', '.join(evos)}_ "
    if found_heroes: text += f"\n🦸 Héros : *{', '.join(found_heroes)}*"
    return text

async def make_deck_grid(cards: list) -> BytesIO:
    cards = list(cards)[:8]
    imgs = []
    async with httpx.AsyncClient(timeout=8) as c:
        for card in cards:
            url = card.get("iconUrls", {}).get("evolutionMedium") or card.get("iconUrls", {}).get("medium", "")
            try:
                r = await c.get(url)
                img = Image.open(BytesIO(r.content)).convert("RGBA").resize((CARD_W, CARD_H))
            except:
                img = Image.new("RGBA", (CARD_W, CARD_H), (40, 40, 60, 255))
            imgs.append(img)

    canvas = Image.new("RGB", (4 * CARD_W + 5 * GAP, 2 * CARD_H + 3 * GAP), BG_COLOR)
    for i, img in enumerate(imgs):
        x, y = GAP + (i % 4) * (CARD_W + GAP), GAP + (i // 4) * (CARD_H + GAP)
        canvas.paste(img, (x, y), img if img.mode == "RGBA" else None)
    buf = BytesIO(); canvas.save(buf, "PNG"); buf.seek(0)
    return buf

# ─── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Clash Royale Scout prêt ! Utilisez /deck [Pseudo].")

async def cmd_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/deck Pseudo`")
        return
    query = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(f"🔍 Recherche de `{query}`...")
    
    results = await search_players(query)
    if not results:
        await msg.edit_text("❌ Aucun joueur trouvé.")
        return

    keyboard = []
    for p in results[:8]:
        btn_text = f"{p.get('name')} | {p.get('clan',{}).get('name','Sans clan')} | 🏆{p.get('trophies')}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"deck:{p['tag']}")])
    await msg.edit_text("Sélectionnez le joueur :", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tag = q.data.split(":")[1]
    player = await get_player(tag)
    battles = await get_battles(tag)
    is_uc, max_elo = uc_info(player)
    
    recap = (f"👤 *{player.get('name')}* (`{tag}`)\n"
             f"🏆 Trophées : {player.get('trophies')}\n"
             f"{'👑 *Champion Suprême* (Max ELO: `' + str(max_elo) + '`)\n' if is_uc else ''}")

    deck_cards = []
    if battles:
        for p in battles[0].get("team", []):
            if p.get("tag") == tag: deck_cards = p.get("cards", [])

    await q.message.reply_text(recap, parse_mode="Markdown")
    if deck_cards:
        grid = await make_deck_grid(deck_cards)
        await q.message.reply_photo(photo=grid, caption=get_deck_info(deck_cards), parse_mode="Markdown")
    await q.delete_message()

# ─── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("deck", cmd_deck))
    app.add_handler(CallbackQueryHandler(callback_deck, pattern=r"^deck:"))
    print("Bot démarré !")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
