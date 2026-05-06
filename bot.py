import os
import json
import logging
import asyncio
import base64
from io import BytesIO

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ─── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
ROYALE_API_KEY   = os.getenv("ROYALE_API_KEY", "")
GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_KEY", "")  # optionnel pour OCR
ROYALE_BASE      = "https://api.clashroyale.com/v1"
DATA_FILE        = "users.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ─── Persistance utilisateurs ───────────────────────────────────────────────────
def load_users() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_users(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─── Appels API Clash Royale ────────────────────────────────────────────────────
HEADERS = {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str) -> dict | None:
    url = f"{ROYALE_BASE}{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=HEADERS)
        if r.status_code == 200:
            return r.json()
        log.error(f"API error {r.status_code} for {path}")
        return None

def encode_tag(tag: str) -> str:
    """#ABC123 → %23ABC123"""
    return tag.strip().upper().replace("#", "%23")

async def get_player(tag: str) -> dict | None:
    return await api_get(f"/players/{encode_tag(tag)}")

async def get_battles(tag: str) -> list | None:
    data = await api_get(f"/players/{encode_tag(tag)}/battlelog")
    return data if isinstance(data, list) else None

async def search_player_by_name(name: str) -> list:
    data = await api_get(f"/players?name={name}&limit=5")
    if data and "items" in data:
        return data["items"]
    return []

# ─── OCR via Google Vision ──────────────────────────────────────────────────────
async def ocr_image(image_bytes: bytes) -> str | None:
    if not GOOGLE_VISION_KEY:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "requests": [{
            "image": {"content": b64},
            "features": [{"type": "TEXT_DETECTION"}]
        }]
    }
    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_KEY}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        if r.status_code == 200:
            data = r.json()
            texts = data.get("responses", [{}])[0].get("textAnnotations", [])
            if texts:
                return texts[0].get("description", "")
    return None

# ─── Formatage des stats ────────────────────────────────────────────────────────
ELIXIR_EMOJI = {1: "⚡", 2: "⚡⚡", 3: "⚡⚡⚡", 4: "⚡⚡⚡⚡", 5: "⚡⚡⚡⚡⚡",
                6: "⚡x6", 7: "⚡x7", 8: "⚡x8", 9: "⚡x9"}

def format_deck(cards: list, title: str = "Deck") -> str:
    if not cards:
        return f"*{title}* : inconnu"
    lines = [f"*{title}*"]
    avg = sum(c.get("elixirCost", 0) for c in cards) / max(len(cards), 1)
    for c in cards:
        cost = c.get("elixirCost", "?")
        name = c.get("name", "?")
        lvl  = c.get("level", "?")
        lines.append(f"  {ELIXIR_EMOJI.get(cost, '⚡')} `{name}` (niv.{lvl})")
    lines.append(f"  📊 Coût moyen : `{avg:.1f}`")
    return "\n".join(lines)

def compute_streak(battles: list, tag: str) -> tuple[int, str]:
    """Retourne (streak_count, 'W'/'L')"""
    clean_tag = tag.upper().replace("#", "")
    streak = 0
    current = None
    for b in battles:
        team_tags = [p.get("tag", "").replace("#","") for p in b.get("team", [])]
        if clean_tag not in team_tags:
            continue
        # Chercher si on a gagné
        team_crowns = sum(p.get("crowns", 0) for p in b.get("team", []))
        opp_crowns  = sum(p.get("crowns", 0) for p in b.get("opponent", []))
        result = "W" if team_crowns > opp_crowns else "L"
        if current is None:
            current = result
        if result == current:
            streak += 1
        else:
            break
    return streak, current or "?"

def compute_winrate(battles: list, tag: str, n: int = 25) -> float:
    clean_tag = tag.upper().replace("#", "")
    wins = 0
    total = 0
    for b in battles[:n]:
        team_tags = [p.get("tag", "").replace("#","") for p in b.get("team", [])]
        if clean_tag not in team_tags:
            continue
        team_crowns = sum(p.get("crowns", 0) for p in b.get("team", []))
        opp_crowns  = sum(p.get("crowns", 0) for p in b.get("opponent", []))
        if team_crowns > opp_crowns:
            wins += 1
        total += 1
    return (wins / total * 100) if total else 0.0

def matchup_analysis(my_cards: list, opp_cards: list) -> str:
    """Analyse basique des avantages de coût d'élixir."""
    if not my_cards or not opp_cards:
        return "Analyse impossible (données manquantes)"
    my_avg  = sum(c.get("elixirCost", 0) for c in my_cards) / max(len(my_cards), 1)
    opp_avg = sum(c.get("elixirCost", 0) for c in opp_cards) / max(len(opp_cards), 1)
    diff = my_avg - opp_avg
    if diff > 0.5:
        return f"⚠️ Ton deck est plus lourd ({my_avg:.1f} vs {opp_avg:.1f}) — joue patient, attends le double élixir"
    elif diff < -0.5:
        return f"✅ Ton deck est plus rapide ({my_avg:.1f} vs {opp_avg:.1f}) — presse dès le début"
    else:
        return f"⚖️ Decks équilibrés ({my_avg:.1f} vs {opp_avg:.1f}) — la technique primera"

# ─── Commandes Telegram ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏆 *Clash Royale Scout Bot*\n\n"
        "Commandes disponibles :\n"
        "• `/setme #TAG` — enregistre ton tag une fois\n"
        "• `/lastgame` — analyse ta dernière partie\n"
        "• `/deck @pseudo` — deck d'un joueur par pseudo\n"
        "• 📸 *Envoie un screenshot* — détection OCR du pseudo\n\n"
        "_Commence par `/setme #TONTAG`_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_setme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/setme #TONTAG`", parse_mode="Markdown")
        return
    tag = ctx.args[0].upper()
    if not tag.startswith("#"):
        tag = "#" + tag

    # Vérifier que le tag existe
    msg = await update.message.reply_text("🔍 Vérification de ton tag...")
    player = await get_player(tag)
    if not player:
        await msg.edit_text("❌ Tag introuvable. Vérifie et réessaie.")
        return

    users = load_users()
    uid   = str(update.effective_user.id)
    users[uid] = {"tag": tag, "name": player.get("name", "?")}
    save_users(users)

    await msg.edit_text(
        f"✅ Tag enregistré !\n"
        f"👤 *{player.get('name')}* — 🏆 {player.get('trophies')} trophées",
        parse_mode="Markdown"
    )

async def cmd_lastgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = str(update.effective_user.id)
    users = load_users()
    if uid not in users:
        await update.message.reply_text("❌ Enregistre d'abord ton tag avec `/setme #TAG`", parse_mode="Markdown")
        return

    my_tag = users[uid]["tag"]
    msg    = await update.message.reply_text("⏳ Récupération de ta dernière partie...")

    battles = await get_battles(my_tag)
    if not battles:
        await msg.edit_text("❌ Impossible de récupérer tes batailles.")
        return

    last = battles[0]
    team     = last.get("team", [{}])[0]
    opponent = last.get("opponent", [{}])[0]

    my_cards   = team.get("cards", [])
    opp_cards  = opponent.get("cards", [])
    opp_tag    = opponent.get("tag", "")
    opp_name   = opponent.get("name", "Inconnu")
    opp_crowns = opponent.get("crowns", 0)
    my_crowns  = team.get("crowns", 0)
    result_emoji = "✅ Victoire" if my_crowns > opp_crowns else "❌ Défaite"

    # Stats adversaire
    opp_battles  = await get_battles(opp_tag) if opp_tag else []
    opp_winrate  = compute_winrate(opp_battles, opp_tag) if opp_battles else None
    opp_streak, opp_streak_type = compute_streak(opp_battles, opp_tag) if opp_battles else (0, "?")
    streak_emoji = "🔥" if opp_streak_type == "W" else "❄️"

    # Construction du message
    lines = [
        f"*Dernière partie — {result_emoji}*",
        f"Couronnes : {my_crowns} — {opp_crowns}",
        "",
        f"👤 Adversaire : *{opp_name}*",
    ]
    if opp_winrate is not None:
        lines.append(f"📈 Winrate (25 dernières) : `{opp_winrate:.1f}%`")
    if opp_streak > 0:
        lines.append(f"{streak_emoji} Streak : `{opp_streak} {opp_streak_type}`")
    lines += [
        "",
        format_deck(opp_cards, "🃏 Deck adversaire"),
        "",
        format_deck(my_cards, "🛡️ Ton deck"),
        "",
        f"🔍 *Matchup* : {matchup_analysis(my_cards, opp_cards)}"
    ]

    await msg.edit_text("\n".join(lines), parse_mode="Markdown")

async def cmd_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/deck NomDuJoueur` ou `/deck #TAG`", parse_mode="Markdown")
        return

    query = " ".join(ctx.args).strip()
    msg   = await update.message.reply_text(f"🔍 Recherche de `{query}`...", parse_mode="Markdown")

    # Tag direct
    if query.startswith("#") or (len(query) > 3 and query[0] not in "@"):
        tag = query if query.startswith("#") else "#" + query
        player = await get_player(tag)
        if player:
            await _show_player_deck(msg, player)
            return

    # Recherche par nom
    name = query.lstrip("@")
    results = await search_player_by_name(name)
    if not results:
        await msg.edit_text("❌ Aucun joueur trouvé.")
        return
    if len(results) == 1:
        await _show_player_deck(msg, results[0])
        return

    # Plusieurs résultats → boutons de confirmation
    keyboard = []
    for p in results[:5]:
        label = f"{p.get('name')} • {p.get('clan', {}).get('name', 'Sans clan')} • 🏆{p.get('trophies')}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"deck:{p.get('tag')}")])

    await msg.edit_text(
        "🔍 Plusieurs joueurs trouvés, lequel ?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tag   = query.data.split(":", 1)[1]
    msg   = await query.edit_message_text("⏳ Chargement du deck...")
    player = await get_player(tag)
    if player:
        await _show_player_deck(msg, player)
    else:
        await msg.edit_text("❌ Impossible de récupérer ce joueur.")

async def _show_player_deck(msg, player: dict):
    cards = player.get("currentDeck", [])
    name  = player.get("name", "?")
    tag   = player.get("tag", "?")
    clan  = player.get("clan", {}).get("name", "Sans clan")
    trophies = player.get("trophies", "?")
    text  = (
        f"👤 *{name}* (`{tag}`)\n"
        f"🏰 Clan : {clan} — 🏆 {trophies}\n\n"
        f"{format_deck(cards, '🃏 Deck actif')}"
    )
    await msg.edit_text(text, parse_mode="Markdown")

# ─── OCR sur screenshot ─────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_VISION_KEY:
        await update.message.reply_text(
            "⚠️ OCR non configuré (GOOGLE_VISION_KEY manquant).\n"
            "Utilise `/deck NomDuJoueur` à la place."
        )
        return

    msg  = await update.message.reply_text("📸 Analyse du screenshot...")
    file = await update.message.photo[-1].get_file()
    buf  = BytesIO()
    await file.download_to_memory(buf)
    text = await ocr_image(buf.getvalue())

    if not text:
        await msg.edit_text("❌ Impossible de lire le texte dans l'image.")
        return

    # Extraire les lignes non vides comme candidats pseudo
    lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 2]
    if not lines:
        await msg.edit_text("❌ Aucun texte détecté dans l'image.")
        return

    # Chercher chaque ligne candidate
    await msg.edit_text(f"🔍 Texte détecté, recherche en cours...")
    for candidate in lines[:5]:
        results = await search_player_by_name(candidate)
        if results:
            if len(results) == 1:
                await _show_player_deck(msg, results[0])
            else:
                keyboard = []
                for p in results[:4]:
                    label = f"{p.get('name')} • {p.get('clan', {}).get('name', 'Sans clan')} • 🏆{p.get('trophies')}"
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"deck:{p.get('tag')}")])
                await msg.edit_text(
                    f"🔍 Pseudo détecté : *{candidate}*\nLequel est ton adversaire ?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            return

    await msg.edit_text(
        f"❌ Aucun joueur trouvé pour les textes détectés :\n`{'`, `'.join(lines[:3])}`\n\n"
        "Essaie `/deck NomExact`",
        parse_mode="Markdown"
    )

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
