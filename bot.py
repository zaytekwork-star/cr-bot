import os
import json
import logging
import urllib.parse
from io import BytesIO

import httpx
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
ROYALE_API_KEY    = os.getenv("ROYALE_API_KEY", "")
ROYALE_BASE       = "https://api.clashroyale.com/v1"

CARD_W, CARD_H = 128, 128
GAP            = 8
BG_COLOR       = (20, 20, 32)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── FONCTIONS API ─────────────────────────────────────────────────────────────
def hdrs():
    return {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str):
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{ROYALE_BASE}{path}", headers=hdrs())
            if r.status_code == 200:
                return r.json()
            log.error(f"Erreur API {r.status_code} sur {path}")
            return None
        except Exception as e:
            log.error(f"Erreur connexion API: {e}")
            return None

async def get_player(tag: str):
    clean_tag = tag.strip().upper().replace("#", "%23")
    return await api_get(f"/players/{clean_tag}")

async def get_battles(tag: str):
    clean_tag = tag.strip().upper().replace("#", "%23")
    d = await api_get(f"/players/{clean_tag}/battlelog")
    return d if isinstance(d, list) else []

async def search_players(name: str):
    if len(name) < 3: return []
    safe_name = urllib.parse.quote(name)
    d = await api_get(f"/players?name={safe_name}&limit=10")
    return d.get("items", []) if d else []

# ─── LOGIQUE DE TRAITEMENT ─────────────────────────────────────────────────────
def uc_info(player: dict):
    """Détecte si le joueur est champion suprême et son max ELO."""
    is_uc = False
    max_elo = 0
    # Vérification dans les badges
    for b in player.get("badges", []):
        if "ultimatechampion" in b.get("name", "").lower():
            is_uc = True
            max_elo = max(max_elo, b.get("progress", 0), b.get("value", 0))
    # Vérification dans les stats de ligue
    best = player.get("leagueStatistics", {}).get("bestSeason", {})
    if best:
        is_uc = True
        max_elo = max(max_elo, best.get("trophies", 0))
    return is_uc, max_elo

def get_deck_info(cards: list):
    """Génère le texte sous la photo du deck (épuré)."""
    if not cards: return "Deck inconnu"
    avg = sum(c.get("elixirCost", 0) for c in cards) / 8
    
    # Détection Évos et Héros
    evos = [c.get("name") for c in cards if "evolution" in c.get("iconUrls", {})]
    heroes_list = ["Little Prince", "Archer Queen", "Golden Knight", "Skeleton King", "Mighty Miner", "Monk"]
    found_heroes = [c.get("name") for c in cards if c.get("name") in heroes_list]
    
    text = f"⚡ Coût moyen : `{avg:.1f}`"
    if evos: text += f"\n🧬 Évolutions : _{', '.join(evos)}_ "
    if found_heroes: text += f"\n🦸 Héros : *{', '.join(found_heroes)}*"
    return text

async def make_deck_grid(cards: list):
    """Crée l'image 4x2 du deck."""
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

    W = 4 * CARD_W + 5 * GAP
    H = 2 * CARD_H + 3 * GAP
    canvas = Image.new("RGB", (W, H), BG_COLOR)
    for i, img in enumerate(imgs):
        x = GAP + (i % 4) * (CARD_W + GAP)
        y = GAP + (i // 4) * (CARD_H + GAP)
        canvas.paste(img, (x, y), img if img.mode == "RGBA" else None)
    
    buf = BytesIO(); canvas.save(buf, "PNG"); buf.seek(0)
    return buf

# ─── HANDLERS TELEGRAM ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Bot Clash Royale prêt !\nUtilise `/deck NomDuJoueur` pour commencer.")

async def cmd_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/deck Pseudo` ou `/deck #TAG`")
        return

    query = " ".join(ctx.args).strip()
    msg = await update.message.reply_text(f"🔍 Recherche de `{query}`...")

    # Si c'est un Tag
    if query.startswith("#"):
        p = await get_player(query)
        if p:
            await msg.delete()
            await show_player_profile(update, p)
        else:
            await msg.edit_text("❌ Joueur introuvable avec ce Tag.")
        return

    # Recherche par nom
    results = await search_players(query)
    if not results:
        await msg.edit_text("❌ Aucun joueur trouvé avec ce nom.")
        return

    keyboard = []
    for p in results[:8]:
        clan = p.get('clan', {}).get('name', 'Sans clan')
        btn_text = f"{p.get('name')} | {clan} | 🏆{p.get('trophies')}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"deck:{p['tag']}")])
    
    await msg.edit_text("Sélectionnez le bon joueur :", reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tag = query.data.split(":")[1]
    await query.answer("Chargement du profil...")
    player = await get_player(tag)
    await query.message.delete()
    await show_player_profile(update, player)

async def show_player_profile(update: Update, player: dict):
    tag = player.get("tag")
    battles = await get_battles(tag)
    is_uc, max_elo = uc_info(player)
    
    recap = (f"👤 *{player.get('name')}* (`{tag}`)\n"
             f"🏰 Clan : {player.get('clan', {}).get('name', 'Sans clan')}\n"
             f"🏆 Trophées : {player.get('trophies')}\n")
    
    if is_uc:
        recap += f"👑 *Champion Suprême* (Max ELO: `{max_elo}`)\n"

    # Trouver le dernier deck utilisé
    deck_cards = []
    if battles:
        for p in battles[0].get("team", []):
            if p.get("tag") == tag:
                deck_cards = p.get("cards", [])

    await update.effective_message.reply_text(recap, parse_mode="Markdown")
    
    if deck_cards:
        grid = await make_deck_grid(deck_cards)
        await update.effective_message.reply_photo(
            photo=grid,
            caption=get_deck_info(deck_cards),
            parse_mode="Markdown"
        )

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN or not ROYALE_API_KEY:
        print("ERREUR: Token Telegram ou Clé Royale manquante dans les variables d'environnement.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("deck", cmd_deck))
    app.add_handler(CallbackQueryHandler(callback_deck, pattern=r"^deck:"))
    
    print("Bot démarré avec succès ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
