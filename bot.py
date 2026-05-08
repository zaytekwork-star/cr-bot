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

# ─── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
ROYALE_API_KEY    = os.getenv("ROYALE_API_KEY", "")
GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_KEY", "")
ROYALE_BASE       = "https://api.clashroyale.com/v1"
DATA_FILE         = "users.json"

# Grille 4×2
CARD_W, CARD_H = 128, 128
GAP            = 8
BG_COLOR       = (20, 20, 32)

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

# ─── API ───────────────────────────────────────────────────────────────────────
def hdrs():
    return {"Authorization": f"Bearer {ROYALE_API_KEY}"}

async def api_get(path: str):
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{ROYALE_BASE}{path}", headers=hdrs())
        if r.status_code == 200:
            return r.json()
        log.error(f"API {r.status_code} {path}: {r.text[:100]}")
        return None

def etag(tag: str) -> str:
    return tag.strip().upper().replace("#", "%23")

async def get_player(tag: str):
    return await api_get(f"/players/{etag(tag)}")

async def get_battles(tag: str) -> list:
    d = await api_get(f"/players/{etag(tag)}/battlelog")
    return d if isinstance(d, list) else []

async def search_players(name: str) -> list:
    d = await api_get(f"/players?name={name}&limit=10")
    return d.get("items", []) if d and "items" in d else []

# ─── Grille 4×2 ────────────────────────────────────────────────────────────────
async def fetch_card_img(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url)
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content)).convert("RGBA")
                return img.resize((CARD_W, CARD_H), Image.LANCZOS)
    except Exception as e:
        log.warning(f"card img error: {e}")
    return None

async def make_deck_grid(cards: list) -> BytesIO:
    """Grille 4 colonnes × 2 rangées, toutes cartes à la même taille."""
    cards = list(cards)[:8]
    while len(cards) < 8:
        cards.append({})

    imgs = []
    for card in cards:
        url = card.get("iconUrls", {}).get("medium", "")
        img = await fetch_card_img(url)
        if img is None:
            # placeholder sombre
            img = Image.new("RGBA", (CARD_W, CARD_H), (40, 40, 60, 255))
        imgs.append(img)

    cols, rows = 4, 2
    W = cols * CARD_W + (cols + 1) * GAP
    H = rows * CARD_H + (rows + 1) * GAP
    canvas = Image.new("RGB", (W, H), BG_COLOR)

    for i, img in enumerate(imgs):
        col = i % cols
        row = i // cols
        x   = GAP + col * (CARD_W + GAP)
        y   = GAP + row * (CARD_H + GAP)
        if img.mode == "RGBA":
            canvas.paste(img, (x, y), img)
        else:
            canvas.paste(img, (x, y))

    buf = BytesIO()
    canvas.save(buf, "PNG")
    buf.seek(0)
    return buf

# ─── Stats ─────────────────────────────────────────────────────────────────────
def winrate(battles: list, tag: str, n: int = 25) -> tuple[float, int, int]:
    clean = tag.upper().replace("#", "")
    w = t = 0
    for b in battles[:n]:
        ttags = [p.get("tag","").replace("#","") for p in b.get("team",[])]
        if clean not in ttags:
            continue
        tc = sum(p.get("crowns",0) for p in b.get("team",[]))
        oc = sum(p.get("crowns",0) for p in b.get("opponent",[]))
        if tc > oc:
            w += 1
        t += 1
    return (w/t*100 if t else 0.0), w, t

def streak(battles: list, tag: str) -> tuple[int, str]:
    clean = tag.upper().replace("#","")
    s = 0
    cur = None
    for b in battles:
        ttags = [p.get("tag","").replace("#","") for p in b.get("team",[])]
        if clean not in ttags:
            continue
        tc = sum(p.get("crowns",0) for p in b.get("team",[]))
        oc = sum(p.get("crowns",0) for p in b.get("opponent",[]))
        res = "W" if tc > oc else "L"
        if cur is None:
            cur = res
        if res == cur:
            s += 1
        else:
            break
    return s, cur or "?"

def uc_info(player: dict) -> tuple[bool, int]:
    """Ultimate Champion + max ELO depuis les badges et leagueStatistics."""
    # Via badges
    for b in player.get("badges", []):
        if "ultimatechampion" in b.get("name","").lower():
            return True, b.get("maxLevel", 0)
    # Via leagueStatistics
    best = player.get("leagueStatistics",{}).get("bestSeason",{})
    elo  = best.get("trophies", 0)
    return (elo >= 9000, elo)

def matchup_analysis(my_cards: list, opp_cards: list) -> str:
    if not my_cards or not opp_cards:
        return "_Données insuffisantes_"
    my_avg  = sum(c.get("elixirCost",0) for c in my_cards) / max(len(my_cards),1)
    opp_avg = sum(c.get("elixirCost",0) for c in opp_cards) / max(len(opp_cards),1)
    diff = my_avg - opp_avg
    if diff > 1.0:
        tip, est = "⚠️ Tu es bien plus lourd — attends le double élixir", "~42%"
    elif diff > 0.4:
        tip, est = "🔶 Légèrement plus lourd — joue défensif et contre-attaque", "~47%"
    elif diff < -1.0:
        tip, est = "✅ Tu cycles bien plus vite — presse dès le début", "~58%"
    elif diff < -0.4:
        tip, est = "🔷 Légèrement plus rapide — petit avantage en early", "~53%"
    else:
        tip, est = "⚖️ Decks équilibrés — technique et timing décideront", "~50%"
    return f"{tip}\n  Winrate estimé : *{est}* _(toi `{my_avg:.1f}⚡` vs lui `{opp_avg:.1f}⚡`)_"

def deck_text(cards: list) -> str:
    if not cards:
        return "_Deck inconnu_"
    avg = sum(c.get("elixirCost",0) for c in cards) / max(len(cards),1)
    lines = [f"⚡ *Coût moyen :* `{avg:.2f}`\n"]
    for c in cards:
        cost = c.get("elixirCost","?")
        name = c.get("name","?")
        lvl  = c.get("level","?")
        lines.append(f"`{str(cost).rjust(2)}⚡` {name} _(niv.{lvl})_")
    return "\n".join(lines)

def last_deck_of(battles: list, tag: str) -> list:
    """Extrait le deck joué par ce tag dans sa dernière bataille."""
    clean = tag.upper().replace("#","")
    if not battles:
        return []
    last = battles[0]
    for side in ["team","opponent"]:
        for p in last.get(side,[]):
            if p.get("tag","").upper().replace("#","") == clean:
                return p.get("cards",[])
    return []

# ─── Commandes ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 *Clash Royale Scout Bot*\n\n"
        "• `/setme #TAG` — enregistre ton tag\n"
        "• `/lastgame` — analyse ta dernière partie\n"
        "• `/deck NomJoueur` — scout un adversaire\n"
        "• 📸 Screenshot — OCR du pseudo\n\n"
        "_Commence par `/setme #TONTAG`_",
        parse_mode="Markdown"
    )

async def cmd_setme(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : `/setme #TONTAG`", parse_mode="Markdown")
        return
    tag = ctx.args[0].upper().strip()
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
    msg    = await update.message.reply_text("⏳ Chargement...")

    battles = await get_battles(my_tag)
    if not battles:
        await msg.edit_text("❌ Impossible de récupérer les batailles.")
        return

    last       = battles[0]
    team       = last.get("team",[{}])[0]
    opp        = last.get("opponent",[{}])[0]
    my_cards   = team.get("cards",[])
    opp_cards  = opp.get("cards",[])
    opp_tag    = opp.get("tag","")
    opp_name   = opp.get("name","Inconnu")
    my_crowns  = team.get("crowns",0)
    opp_crowns = opp.get("crowns",0)
    result     = "✅ Victoire" if my_crowns > opp_crowns else "❌ Défaite"

    # Stats adversaire
    opp_battles      = await get_battles(opp_tag) if opp_tag else []
    opp_player       = await get_player(opp_tag) if opp_tag else {}
    opp_wr, ow, ot   = winrate(opp_battles, opp_tag)
    opp_s, opp_st    = streak(opp_battles, opp_tag)
    is_uc, max_elo   = uc_info(opp_player) if opp_player else (False, 0)
    streak_icon      = "🔥" if opp_st == "W" else "❄️"
    uc_line          = f"👑 Ultimate Champion — Max ELO `{max_elo}`\n" if is_uc else ""

    # Mes stats
    my_wr, mw, mt = winrate(battles, my_tag)

    await msg.delete()

    # Résumé
    await update.message.reply_text(
        f"*{result}* — {my_crowns} 👑 vs {opp_crowns} 👑\n\n"
        f"👤 *{opp_name}*\n"
        f"{uc_line}"
        f"📈 Winrate adversaire : `{opp_wr:.1f}%` ({ow}V/{ot-ow}D)\n"
        f"{streak_icon} Streak : `{opp_s} {'victoires' if opp_st=='W' else 'défaites'}`\n\n"
        f"🛡️ Ton winrate (25 dernières) : `{my_wr:.1f}%` ({mw}V/{mt-mw}D)\n\n"
        f"🔍 *Matchup*\n{matchup_analysis(my_cards, opp_cards)}",
        parse_mode="Markdown"
    )

    # Deck adversaire — grille 4×2
    grid = await make_deck_grid(opp_cards)
    await update.message.reply_photo(
        photo=grid,
        caption=f"🃏 *Deck de {opp_name}*\n\n{deck_text(opp_cards)}",
        parse_mode="Markdown"
    )

    # Mon deck — grille 4×2
    my_grid = await make_deck_grid(my_cards)
    await update.message.reply_photo(
        photo=my_grid,
        caption=f"🛡️ *Ton deck*\n\n{deck_text(my_cards)}",
        parse_mode="Markdown"
    )

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
            await _show_opponent(update, player)
        else:
            await msg.edit_text("❌ Tag introuvable.")
        return

    # Recherche par nom (case-insensitive)
    results = await search_players(query)
    q_low   = query.lower()
    exact   = [p for p in results if p.get("name","").lower() == q_low]
    pool    = exact if exact else results

    if not pool:
        await msg.edit_text(
            f"❌ Aucun joueur trouvé pour `{query}`.\nEssaie avec le tag : `/deck #XXXX`",
            parse_mode="Markdown"
        )
        return

    if len(pool) == 1:
        player = await get_player(pool[0]["tag"])
        if player:
            await msg.delete()
            await _show_opponent(update, player)
        return

    # Plusieurs résultats → boutons avec clan
    keyboard = []
    for p in pool[:8]:
        clan  = p.get("clan",{}).get("name","Sans clan")
        name  = p.get("name","?")
        tag   = p.get("tag","")
         trophy = p.get("trophies","?")
        keyboard.append([InlineKeyboardButton(
            f"{name}  |  {clan}  |  🏆{ trophy}",
            callback_data=f"deck:{tag}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="deck:cancel")])

    await msg.edit_text(
        f"🔍 *{len(pool)} joueurs trouvés*\nSélectionne l'adversaire :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def callback_deck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tag = q.data.split(":",1)[1]
    if tag == "cancel":
        await q.edit_message_text("❌ Annulé.")
        return
    await q.edit_message_text("⏳ Chargement...")
    player = await get_player(tag)
    if not player:
        await q.edit_message_text("❌ Joueur introuvable.")
        return
    await q.delete_message()
    await _show_opponent(update, player)

async def _show_opponent(update: Update, player: dict):
    uid   = str(update.effective_user.id)
    users = load_users()
    tag      = player.get("tag","")
    name     = player.get("name","?")
    clan     = player.get("clan",{}).get("name","Sans clan")
    trophies = player.get("trophies","?")

    battles  = await get_battles(tag)
    opp_deck = last_deck_of(battles, tag)

    wr, w, t       = winrate(battles, tag)
    is_uc, max_elo = uc_info(player)
    uc_line        = f"👑 Ultimate Champion — Max ELO `{max_elo}`\n" if is_uc else ""

    # Matchup vs mon deck
    matchup_line = ""
    if uid in users:
        my_battles = await get_battles(users[uid]["tag"])
        my_deck    = last_deck_of(my_battles, users[uid]["tag"])
        if my_deck and opp_deck:
            matchup_line = f"\n🔍 *Matchup vs ton deck*\n{matchup_analysis(my_deck, opp_deck)}"

    recap = (
        f"👤 *{name}* (`{tag}`)\n"
        f"🏰 Clan : {clan} — 🏆 {trophies}\n"
        f"{uc_line}"
        f"📈 Winrate (25 dernières) : `{wr:.1f}%` ({w}V/{t-w}D)"
        f"{matchup_line}"
    )
    await update.effective_message.reply_text(recap, parse_mode="Markdown")

    if opp_deck:
        grid = await make_deck_grid(opp_deck)
        await update.effective_message.reply_photo(
            photo=grid,
            caption=f"🃏 *Dernier deck de {name}*\n\n{deck_text(opp_deck)}",
            parse_mode="Markdown"
        )
    else:
        await update.effective_message.reply_text("⚠️ Deck de la dernière partie introuvable.")

# ─── OCR screenshot ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_VISION_KEY:
        await update.message.reply_text("⚠️ OCR non configuré. Utilise `/deck NomJoueur`.", parse_mode="Markdown")
        return
    msg  = await update.message.reply_text("📸 Analyse...")
    file = await update.message.photo[-1].get_file()
    buf  = BytesIO()
    await file.download_to_memory(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_KEY}",
            json={"requests":[{"image":{"content":b64},"features":[{"type":"TEXT_DETECTION"}]}]}
        )
    if r.status_code != 200:
        await msg.edit_text("❌ Erreur OCR.")
        return
    texts = r.json().get("responses",[{}])[0].get("textAnnotations",[])
    if not texts:
        await msg.edit_text("❌ Aucun texte détecté.")
        return
    lines = [l.strip() for l in texts[0].get("description","").split("\n") if len(l.strip()) > 2]
    await msg.edit_text("🔍 Recherche en cours...")
    for candidate in lines[:5]:
        results = await search_players(candidate)
        if not results:
            continue
        if len(results) == 1:
            await msg.delete()
            player = await get_player(results[0]["tag"])
            if player:
                await _show_opponent(update, player)
            return
        keyboard = []
        for p in results[:6]:
            clan  = p.get("clan",{}).get("name","Sans clan")
            label = f"{p.get('name')}  |  {clan}  |  🏆{p.get('trophies')}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"deck:{p.get('tag')}")])
        keyboard.append([InlineKeyboardButton("❌ Annuler", callback_data="deck:cancel")])
        await msg.edit_text(
            f"🔍 *{candidate}* — Lequel est ton adversaire ?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    await msg.edit_text("❌ Aucun joueur trouvé. Essaie `/deck NomExact`", parse_mode="Markdown")

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
