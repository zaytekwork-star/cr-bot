import os
import json
import logging
import base64
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

# Dimensions Grille 4×2
CARD_W, CARD_H = 128, 128
GAP            = 8
BG_COLOR       = (20, 20, 32)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── Persistance ───────────────────────────────────────────────────────────────
def load_users() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f: return json.load(f)
        except: return {}
    return {}

def save_users(data: dict):
    with open(DATA_FILE, "w") as f: json.dump(data, f, indent=2)

# ─── API Supercell ─────────────────────────────────────────────────────────────
def hdrs():
    return {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{ROYALE_BASE}{path}", headers=hdrs())
        if r.status_code == 200: return r.json()
        log.error(f"API Error {r.status_code} sur {path}")
        return None

def etag(tag: str) -> str:
    return tag.strip().upper().replace("#", "%23")

async def get_player(tag: str):
    return await api_get(f"/players/{etag(tag)}")

async def get_battles(tag: str) -> list:
    d = await api_get(f"/players/{etag(tag)}/battlelog")
    return d if isinstance(d, list) else []

async def search_players(name: str) -> list:
    # On encode le nom pour les espaces/caractères spéciaux
    import urllib.parse
    safe_name = urllib.parse.quote(name)
    d = await api_get(f"/players?name={safe_name}&limit=10")
    return d.get("items", []) if d else []

# ─── Traitement Image ──────────────────────────────────────────────────────────
async def fetch_card_img(url: str) -> Image.Image | None:
    if not url: return None
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url)
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content)).convert("RGBA")
                return img.resize((CARD_W, CARD_H), Image.LANCZOS)
    except: return None

async def make_deck_grid(cards: list) -> BytesIO:
    cards = list(cards)[:8]
    imgs = []
    for card in cards:
        # Priorité à l'icône d'évolution si elle existe, sinon medium
        url = card.get("iconUrls", {}).get("evolutionMedium") or card.get("iconUrls", {}).get("medium", "")
        img = await fetch_card_img(url)
        imgs.append(img or Image.new("RGBA", (CARD_W, CARD_H), (40, 40, 60, 255)))

    W = 4 * CARD_W + 5 * GAP
    H = 2 * CARD_H + 3 * GAP
    canvas = Image.new("RGB", (W, H), BG_COLOR)

    for i, img in enumerate(imgs):
        x = GAP + (i % 4) * (CARD_W + GAP)
        y = GAP + (i // 4) * (CARD_H + GAP)
        canvas.paste(img, (x, y), img if img.mode == "RGBA" else None)

    buf = BytesIO()
    canvas.save(buf, "PNG")
    buf.seek(0)
    return buf

# ─── Logique Stats ─────────────────────────────────────────────────────────────
def get_deck_info(cards: list) -> str:
    if not cards: return "Deck inconnu"
    avg = sum(c.get("elixirCost", 0) for c in cards) / 8
    
    evos = [c.get("name") for c in cards if "evolution" in c.get("iconUrls", {}) or c.get("name") in ["Barbarians", "Archer", "Knight"]] # Simplifié pour l'exemple
    heros = [c.get("name") for c in cards if c.get("maxLevel") == 14 and c.get("elixirCost", 0) > 4] # Logique simplifiée Hero
    
    text = f"⚡ Coût moyen : `{avg:.1f}`"
    if evos: text += f"\n🧬 Évos probables : _{', '.join(evos[:2])}_"
    # Filtrage manuel des héros connus pour plus de précision
    heroes_list = ["Little Prince", "Archer Queen", "Golden Knight", "Skeleton King", "Mighty Miner", "Monk"]
    found_heroes = [c.get("name") for c in cards if c.get("name") in heroes_list]
    if found_heroes: text += f"\n🦸 Héros : *{', '.join(found_heroes)}*"
    
    return text

def winrate(battles: list, tag: str) -> tuple:
    clean = tag.upper().replace("#", "")
    w = t = 0
    for b in battles[:25]:
        team = b.get("team", [])
        if any(p.get("tag", "").replace("#", "") == clean for p in team):
            t += 1
            if sum(p.get("crowns", 0) for p in team) > sum(p.get("crowns", 0) for p in b.get("opponent", [])):
                w += 1
    return (w/t*100 if t else 0), w, t

def uc_info(player: dict) -> tuple:
    is_uc = False
    max_elo = 0
    # Vérification dans les badges
    for b in player.get("badges", []):
        if "ultimatechampion" in b.get("name", "").lower():
            is_uc = True
            max_elo = max(max_elo, b.get("progress", 0), b.get("value", 0))
    # Vérification stats de ligue
    best = player.get("leagueStatistics", {}).get("bestSeason", {})
    if best:
        is_uc = True
        max_elo = max(max_elo, best.get("trophies", 0))
    return is_uc, max_elo

# ─── Commandes ─────────────────────────────────────────────────────────────────
async def cmd_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/deck Pseudo` ou `/deck #TAG`")
        return

    query = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(f"🔍 Recherche de `{query}`...")

    if query.startswith("#"):
        p = await get_player(query)
        if p: await _show_opponent(update, p, msg)
        else: await msg.edit_text("❌ Joueur introuvable.")
        return

    results = await search_players(query)
    if not results:
        await msg.edit_text("❌ Aucun joueur trouvé.")
        return

    if len(results) == 1:
        p = await get_player(results[0]["tag"])
        await _show_opponent(update, p, msg)
    else:
        keyboard = []
        for p in results[:8]:
            clan = p.get("clan", {}).get("name", "Sans clan")
            btn_text = f"{p.get('name')} | {clan} | 🏆{p.get('trophies')}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"deck:{p['tag']}")])
        
        await msg.edit_text("Plusieurs joueurs trouvés :", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tag = q.data.split(":")[1]
    await q.answer("Chargement...")
    player = await get_player(tag)
    await q.delete_message()
    await _show_opponent(update, player, None)

async def _show_opponent(update: Update, player: dict, old_msg):
    tag = player.get("tag")
    name = player.get("name")
    battles = await get_battles(tag)
    
    # Stats
    wr, w, t = winrate(battles, tag)
    is_uc, max_elo = uc_info(player)
    
    uc_text = f"👑 *Champion Suprême* (Max ELO: `{max_elo}`)\n" if is_uc else ""
    
    recap = (
        f"👤 *{name}* (`{tag}`)\n"
        f"🏰 Clan : {player.get('clan', {}).get('name', 'Sans clan')}\n"
        f"🏆 Trophées : {player.get('trophies')}\n"
        f"{uc_text}"
        f"📈 Winrate (25 derniers) : `{wr:.1f}%` ({w}V/{t-w}D)"
    )

    deck_cards = []
    if battles:
        # On cherche le deck du joueur dans le dernier combat
        for p in battles[0].get("team", []):
            if p.get("tag") == tag:
                deck_cards = p.get("cards", [])

    if old_msg: await old_msg.delete()
    
    await update.effective_message.reply_text(recap, parse_mode="Markdown")
    
    if deck_cards:
        grid = await make_deck_grid(deck_cards)
        await update.effective_message.reply_photo(
            photo=grid,
            caption=get_deck_info(deck_cards),
            parse_mode="Markdown"
        )

# ─── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("deck", cmd_deck))
    app.add_handler(CallbackQueryHandler(callback_deck, pattern=r"^deck:"))
    # ... garde tes autres handlers (start, setme, etc.) ...
    app.run_polling()

if __name__ == "__main__":
    main()
