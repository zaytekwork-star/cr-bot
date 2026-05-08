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
ROYALE_BASE       = "https://api.clashroyale.com/v1"

CARD_W, CARD_H = 128, 128
GAP            = 8
BG_COLOR       = (20, 20, 32)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── API & Logic ───────────────────────────────────────────────────────────────
def hdrs(): 
    return {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{ROYALE_BASE}{path}", headers=hdrs())
        if r.status_code == 200:
            return r.json()
        log.error(f"Erreur API: {r.status_code} sur {path}")
        return None

async def get_player(tag: str):
    return await api_get(f"/players/{tag.strip().upper().replace('#', '%23')}")

async def get_battles(tag: str):
    d = await api_get(f"/players/{tag.strip().upper().replace('#', '%23')}/battlelog")
    return d if isinstance(d, list) else []

async def search_players(name: str, clan_filter: str = None):
    if len(name) < 3:
        return []
    
    safe_name = urllib.parse.quote(name)
    # On demande 25 résultats pour augmenter les chances de trouver le bon clan
    d = await api_get(f"/players?name={safe_name}&limit=25")
    if not d:
        return []
    
    results = d.get("items", [])
    
    if clan_filter:
        clan_filter = clan_filter.lower().strip()
        results = [
            p for p in results 
            if clan_filter in p.get('clan', {}).get('name', '').lower()
        ]
        
    return results

def uc_info(player: dict):
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

def get_deck_info(cards: list):
    avg = sum(c.get("elixirCost", 0) for c in cards) / 8
    evos = [c.get("name") for c in cards if "evolution" in c.get("iconUrls", {})]
    heroes = ["Little Prince", "Archer Queen", "Golden Knight", "Skeleton King", "Mighty Miner", "Monk"]
    found_h = [c.get("name") for c in cards if c.get("name") in heroes]
    
    text = f"⚡ Coût moyen : `{avg:.1f}`"
    if evos: text += f"\n🧬 Évos : _{', '.join(evos[:2])}_"
    if found_h: text += f"\n🦸 Héros : *{', '.join(found_h)}*"
    return text

async def make_deck_grid(cards: list):
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
    await update.message.reply_text("Bot prêt ! Tapez `/deck Pseudo` ou `/deck Pseudo, Nom du Clan` pour filtrer.")

async def cmd_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/deck Pseudo` ou `/deck Pseudo, Nom du Clan`")
        return
    
    full_query = " ".join(ctx.args)
    
    # Gestion du filtre par clan
    if "," in full_query:
        player_name, clan_name = full_query.split(",", 1)
        player_name = player_name.strip()
        clan_name = clan_name.strip()
    else:
        player_name = full_query.strip()
        clan_name = None

    msg = await update.message.reply_text(f"🔍 Recherche de `{player_name}`...")
    results = await search_players(player_name, clan_name)
    
    if not results:
        hint = "\n\n💡 *Astuce:* L'API de recherche est capricieuse. Essayez d'ajouter le clan ou d'utiliser le #TAG."
        await msg.edit_text(f"❌ Aucun joueur trouvé.{hint}", parse_mode="Markdown")
        return

    keyboard = []
    for p in results[:10]: # On affiche jusqu'à 10 résultats
        clan = p.get('clan', {}).get('name', 'Sans clan')
        btn_text = f"{p.get('name')} | {clan} | 🏆{p.get('trophies')}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"deck:{p['tag']}")])
    
    await msg.edit_text("Sélectionnez le joueur :", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    tag = q.data.split(":")[1]
    player = await get_player(tag)
    if not player:
        await q.message.reply_text("Erreur lors de la récupération des données du joueur.")
        return

    battles = await get_battles(tag)
    is_uc, max_elo = uc_info(player)
    
    recap = (f"👤 *{player.get('name')}* (`{tag}`)\n"
             f"🏆 Trophées : {player.get('trophies')}\n")
    if is_uc:
        recap += f"👑 *Champion Suprême* (Max ELO: `{max_elo}`)\n"

    deck_cards = []
    if battles:
        # On cherche le deck dans le dernier combat enregistré
        for side in ["team", "opponent"]:
            for p in battles[0].get(side, []):
                if p.get("tag") == tag: 
                    deck_cards = p.get("cards", [])

    await q.message.reply_text(recap, parse_mode="Markdown")
    if deck_cards:
        grid = await make_deck_grid(deck_cards)
        await q.message.reply_photo(photo=grid, caption=get_deck_info(deck_cards), parse_mode="Markdown")
    else:
        await q.message.reply_text("Impossible de récupérer le dernier deck (pas de combats récents).")
    
    await q.delete_message()

# ─── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not ROYALE_API_KEY:
        print("ERREUR : TELEGRAM_TOKEN ou ROYALE_API_KEY manquant dans le .env")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("deck", cmd_deck))
    app.add_handler(CallbackQueryHandler(callback_deck, pattern=r"^deck:"))
    
    print("Bot démarré ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
