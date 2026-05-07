import os
import json
import logging
import base64
from io import BytesIO

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
ROYALE_API_KEY    = os.getenv("ROYALE_API_KEY", "")
GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_KEY", "")
ROYALE_BASE       = "https://api.clashroyale.com/v1"
DATA_FILE         = "users.json"

# URL officielle des images de cartes Clash Royale
CARD_IMG_BASE = "https://cdn.clashroyale.com/cards/medium/{slug}.png"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── Persistance ───────────────────────────────────────────────────────────────
def load_users() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_users(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─── API Clash Royale ──────────────────────────────────────────────────────────
def get_headers():
    return {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str) -> dict | list | None:
    url = f"{ROYALE_BASE}{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=get_headers())
        if r.status_code == 200:
            return r.json()
        log.error(f"API error {r.status_code} for {path}: {r.text[:200]}")
        return None

def encode_tag(tag: str) -> str:
    return tag.strip().upper().replace("#", "%23")

async def get_player(tag: str) -> dict | None:
    return await api_get(f"/players/{encode_tag(tag)}")

async def get_battles(tag: str) -> list:
    data = await api_get(f"/players/{encode_tag(tag)}/battlelog")
    return data if isinstance(data, list) else []

async def search_players(name: str) -> list:
    data = await api_get(f"/players?name={name}&limit=8")
    if data and "items" in data:
        return data["items"]
    return []

# ─── Images cartes ─────────────────────────────────────────────────────────────
def card_image_url(card: dict) -> str:
    """Retourne l'URL de l'image d'une carte."""
    icon = card.get("iconUrls", {})
    # L'API retourne directement les URLs des icônes
    return icon.get("medium", "") or icon.get("evolutionMedium", "")

async def download_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code == 200:
                return r.content
    except Exception as e:
        log.error(f"Image download error: {e}")
    return None

async def build_deck_collage(cards: list) -> list[bytes]:
    """Télécharge les images des cartes du deck."""
    images = []
    for card in cards:
        url = card_image_url(card)
        img = await download_image(url)
        if img:
            images.append(img)
    return images

# ─── Stats ─────────────────────────────────────────────────────────────────────
ELIXIR_BAR = {1:"▪️", 2:"▫️▫️", 3:"🟣", 4:"🟣🟣", 5:"🟣🟣🟣",
              6:"🟣🟣🟣🟣", 7:"🟣🟣🟣🟣🟣", 8:"🟣x8", 9:"🟣x9"}

def format_deck_text(cards: list, title: str) -> str:
    if not cards:
        return f"*{title}* : inconnu"
    avg = sum(c.get("elixirCost", 0) for c in cards) / max(len(cards), 1)
    lines = [f"*{title}* — coût moy. `{avg:.1f}⚡`"]
    for c in cards:
        cost = c.get("elixirCost", "?")
        name = c.get("name", "?")
        lvl  = c.get("level", "?")
        bar  = ELIXIR_BAR.get(cost, f"{cost}⚡")
        lines.append(f"  {bar} `{name}` niv.{lvl}")
    return "\n".join(lines)

def compute_winrate(battles: list, tag: str, n: int = 25) -> tuple[float, int, int]:
    clean = tag.upper().replace("#", "")
    wins = total = 0
    for b in battles[:n]:
        team_tags = [p.get("tag","").replace("#","") for p in b.get("team",[])]
        if clean not in team_tags:
            continue
        tc = sum(p.get("crowns",0) for p in b.get("team",[]))
        oc = sum(p.get("crowns",0) for p in b.get("opponent",[]))
        if tc > oc:
            wins += 1
        total += 1
    rate = (wins / total * 100) if total else 0.0
    return rate, wins, total

def compute_streak(battles: list, tag: str) -> tuple[int, str]:
    clean = tag.upper().replace("#", "")
    streak = 0
    current = None
    for b in battles:
        team_tags = [p.get("tag","").replace("#","") for p in b.get("team",[])]
        if clean not in team_tags:
            continue
        tc = sum(p.get("crowns",0) for p in b.get("team",[]))
        oc = sum(p.get("crowns",0) for p in b.get("opponent",[]))
        result = "W" if tc > oc else "L"
        if current is None:
            current = result
        if result == current:
            streak += 1
        else:
            break
    return streak, current or "?"

def matchup_analysis(my_cards: list, opp_cards: list) -> str:
    if not my_cards or not opp_cards:
        return "Données insuffisantes"
    my_avg  = sum(c.get("elixirCost",0) for c in my_cards) / max(len(my_cards),1)
    opp_avg = sum(c.get("elixirCost",0) for c in opp_cards) / max(len(opp_cards),1)
    diff = my_avg - opp_avg
    if diff > 0.8:
        verdict = "⚠️ Tu es plus lourd — attends le double élixir pour attaquer"
    elif diff < -0.8:
        verdict = "✅ Tu es plus rapide — presse dès le début, ne laisse pas respirer"
    elif diff > 0.3:
        verdict = "🔶 Légèrement plus lourd — joue défensif en début de partie"
    elif diff < -0.3:
        verdict = "🔷 Légèrement plus rapide — petit avantage en début de partie"
    else:
        verdict = "⚖️ Decks équilibrés — la technique et le timing feront la différence"
    return f"{verdict}\n  _(toi `{my_avg:.1f}` vs lui `{opp_avg:.1f}`)_"

# ─── Envoi deck avec images ────────────────────────────────────────────────────
async def send_deck_with_images(update_or_msg, cards: list, caption: str, is_callback: bool = False):
    """Envoie les images des cartes en groupe + texte."""
    images = await build_deck_collage(cards)

    if len(images) >= 2:
        media_group = []
        for i, img_bytes in enumerate(images[:8]):
            buf = BytesIO(img_bytes)
            buf.name = f"card_{i}.png"
            if i == 0:
                media_group.append(InputMediaPhoto(media=buf, caption=caption, parse_mode="Markdown"))
            else:
                media_group.append(InputMediaPhoto(media=buf))
        if is_callback:
            chat_id = update_or_msg.message.chat_id
            await update_or_msg.message.reply_media_group(media=media_group)
        else:
            await update_or_msg.message.reply_media_group(media=media_group)
    else:
        # Pas d'images, texte seulement
        if is_callback:
            await update_or_msg.message.reply_text(caption, parse_mode="Markdown")
        else:
            await update_or_msg.message.reply_text(caption, parse_mode="Markdown")

# ─── Commandes ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 *Clash Royale Scout Bot*\n\n"
        "• `/setme #TAG` — enregistre ton tag\n"
        "• `/lastgame` — analyse ta dernière partie\n"
        "• `/deck NomJoueur` — deck d'un adversaire\n"
        "• 📸 Screenshot — détection automatique du pseudo\n\n"
        "_Commence par `/setme #TONTAG`_",
        parse_mode="Markdown"
    )

async def cmd_setme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/setme #TONTAG`", parse_mode="Markdown")
        return
    tag = ctx.args[0].upper()
    if not tag.startswith("#"):
        tag = "#" + tag
    msg = await update.message.reply_text("🔍 Vérification...")
    player = await get_player(tag)
    if not player:
        await msg.edit_text("❌ Tag introuvable.")
        return
    users = load_users()
    users[str(update.effective_user.id)] = {"tag": tag, "name": player.get("name","?")}
    save_users(users)
    await msg.edit_text(
        f"✅ *{player.get('name')}* enregistré !\n🏆 {player.get('trophies')} trophées",
        parse_mode="Markdown"
    )

async def cmd_lastgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = str(update.effective_user.id)
    users = load_users()
    if uid not in users:
        await update.message.reply_text("❌ Fais `/setme #TAG` d'abord.", parse_mode="Markdown")
        return

    my_tag = users[uid]["tag"]
    msg    = await update.message.reply_text("⏳ Récupération en cours...")

    battles = await get_battles(my_tag)
    if not battles:
        await msg.edit_text("❌ Impossible de récupérer les batailles.")
        return

    last     = battles[0]
    team     = last.get("team", [{}])[0]
    opponent = last.get("opponent", [{}])[0]
    my_cards  = team.get("cards", [])
    opp_cards = opponent.get("cards", [])
    opp_tag   = opponent.get("tag", "")
    opp_name  = opponent.get("name", "Inconnu")
    my_crowns  = team.get("crowns", 0)
    opp_crowns = opponent.get("crowns", 0)
    result     = "✅ Victoire" if my_crowns > opp_crowns else "❌ Défaite"

    # Stats adversaire
    opp_battles = await get_battles(opp_tag) if opp_tag else []
    opp_wr, opp_w, opp_t = compute_winrate(opp_battles, opp_tag)
    opp_streak, opp_st   = compute_streak(opp_battles, opp_tag)
    streak_icon = "🔥" if opp_st == "W" else "❄️"

    # Winrate de MON deck sur mes 25 dernières
    my_wr, my_w, my_t = compute_winrate(battles, my_tag)

    await msg.delete()

    # ── Message résumé ──
    summary = (
        f"*{result}* — Couronnes {my_crowns}–{opp_crowns}\n\n"
        f"👤 *{opp_name}*\n"
        f"📈 Winrate adversaire : `{opp_wr:.1f}%` ({opp_w}V/{opp_t-opp_w}D)\n"
        f"{streak_icon} Streak : `{opp_streak} {'victoires' if opp_st=='W' else 'défaites'}`\n\n"
        f"🛡️ Ton winrate actuel : `{my_wr:.1f}%` ({my_w}V/{my_t-my_w}D sur {my_t} parties)\n\n"
        f"🔍 *Matchup*\n{matchup_analysis(my_cards, opp_cards)}"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")

    # ── Deck adversaire avec images ──
    opp_deck_text = format_deck_text(opp_cards, f"🃏 Deck de {opp_name}")
    await send_deck_with_images(update, opp_cards, opp_deck_text)

    # ── Ton deck avec images ──
    my_deck_text = format_deck_text(my_cards, "🛡️ Ton deck")
    await send_deck_with_images(update, my_cards, my_deck_text)

async def cmd_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/deck NomJoueur` ou `/deck #TAG`", parse_mode="Markdown")
        return

    query = " ".join(ctx.args).strip().lstrip("@")
    msg   = await update.message.reply_text(f"🔍 Recherche de `{query}`...", parse_mode="Markdown")

    # Tag direct
    if query.startswith("#"):
        player = await get_player(query)
        if player:
            await msg.delete()
            await _show_deck(update, player)
            return
        await msg.edit_text("❌ Tag introuvable.")
        return

    # Recherche par nom
    results = await search_players(query)
    if not results:
        await msg.edit_text(f"❌ Aucun joueur trouvé pour `{query}`.\nEssaie avec le tag `#XXXX` directement.", parse_mode="Markdown")
        return

    if len(results) == 1:
        await msg.delete()
        await _show_deck(update, results[0])
        return

    # Liste avec clans → boutons
    keyboard = []
    for p in results[:8]:
        clan   = p.get("clan", {}).get("name", "Sans clan")
        trophy = p.get("trophies", "?")
        label  = f"{p.get('name')} • {clan} • 🏆{trophy}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"deck:{p.get('tag')}")])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="deck:cancel")])

    await msg.edit_text(
        f"🔍 *{len(results)} joueurs trouvés pour `{query}`*\nLequel est ton adversaire ?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tag = query.data.split(":", 1)[1]

    if tag == "cancel":
        await query.edit_message_text("❌ Recherche annulée.")
        return

    await query.edit_message_text("⏳ Chargement du deck...")
    player = await get_player(tag)
    if not player:
        await query.edit_message_text("❌ Impossible de récupérer ce joueur.")
        return

    await query.delete_message()
    await _show_deck(update, player)

async def _show_deck(update: Update, player: dict):
    """Affiche le deck actif d'un joueur avec images."""
    cards    = player.get("currentDeck", [])
    name     = player.get("name", "?")
    tag      = player.get("tag", "?")
    clan     = player.get("clan", {}).get("name", "Sans clan")
    trophies = player.get("trophies", "?")

    header = (
        f"👤 *{name}* (`{tag}`)\n"
        f"🏰 {clan} — 🏆 {trophies}\n\n"
    ) + format_deck_text(cards, "🃏 Deck actif")

    await send_deck_with_images(update, cards, header)

# ─── OCR screenshot ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_VISION_KEY:
        await update.message.reply_text(
            "⚠️ OCR non configuré.\nUtilise `/deck NomJoueur` à la place.",
            parse_mode="Markdown"
        )
        return

    msg  = await update.message.reply_text("📸 Analyse du screenshot...")
    file = await update.message.photo[-1].get_file()
    buf  = BytesIO()
    await file.download_to_memory(buf)

    b64 = base64.b64encode(buf.getvalue()).decode()
    payload = {"requests": [{"image": {"content": b64}, "features": [{"type": "TEXT_DETECTION"}]}]}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_KEY}",
            json=payload
        )
    if r.status_code != 200:
        await msg.edit_text("❌ Erreur OCR.")
        return

    texts = r.json().get("responses",[{}])[0].get("textAnnotations",[])
    if not texts:
        await msg.edit_text("❌ Aucun texte détecté.")
        return

    lines = [l.strip() for l in texts[0].get("description","").split("\n") if len(l.strip()) > 2]
    await msg.edit_text("🔍 Texte détecté, recherche...")

    for candidate in lines[:5]:
        results = await search_players(candidate)
        if not results:
            continue

        if len(results) == 1:
            await msg.delete()
            await _show_deck(update, results[0])
            return

        keyboard = []
        for p in results[:6]:
            clan  = p.get("clan",{}).get("name","Sans clan")
            label = f"{p.get('name')} • {clan} • 🏆{p.get('trophies')}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"deck:{p.get('tag')}")])
        keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="deck:cancel")])

        await msg.edit_text(
            f"🔍 Pseudo détecté : *{candidate}*\nLequel est ton adversaire ?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await msg.edit_text("❌ Aucun joueur trouvé.\nEssaie `/deck NomExact`", parse_mode="Markdown")

# ─── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN manquant")
    if not ROYALE_API_KEY:
        raise ValueError("ROYALE_API_KEY manquant")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("setme",    cmd_setme))
    app.add_handler(CommandHandler("lastgame", cmd_lastgame))
    app.add_handler(CommandHandler("deck",     cmd_deck))
    app.add_handler(CallbackQueryHandler(callback_deck, pattern=r"^deck:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Bot démarré ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
