import os, json, smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime

from flask import Flask, render_template, jsonify, request, url_for, redirect, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
import stripe, requests
from dotenv import load_dotenv
from passlib.hash import bcrypt

# =======================
#   CONFIG & INITIALISATION
# =======================
load_dotenv()
app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret')

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///puba.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- Stripe
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRICE_SUB_ID = os.getenv('STRIPE_PRICE_SUB_ID', '')  # prix mensuel/écran
stripe.api_key = STRIPE_SECRET_KEY

# --- PayPal
PAYPAL_CLIENT_ID = os.getenv('PAYPAL_CLIENT_ID', '')
PAYPAL_SECRET = os.getenv('PAYPAL_SECRET', '')
PAYPAL_ENV = os.getenv('PAYPAL_ENV', 'sandbox')
if PAYPAL_ENV == 'live':
    PAYPAL_OAUTH_URL = 'https://api-m.paypal.com/v1/oauth2/token'
    PAYPAL_ORDERS_URL = 'https://api-m.paypal.com/v2/checkout/orders'
else:
    PAYPAL_OAUTH_URL = 'https://api-m.sandbox.paypal.com/v1/oauth2/token'
    PAYPAL_ORDERS_URL = 'https://api-m.sandbox.paypal.com/v2/checkout/orders'

# --- SMTP
SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
SMTP_FROM = os.getenv('SMTP_FROM', 'no-reply@puba.local')
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '')

# --- Produit démo (essai)
PRODUCT = {
    'id': 'puba-001',
    'name': 'Afficheur PUBAJIMJIM - 1 mois',
    'description': "Système d'affichage publicitaire PUBAJIMJIM — formule essai 1 mois",
    'currency': 'eur',
    'amount_cents': 5999  # 59,99 €
}

# =======================
#   BASE DE DONNÉES
# =======================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    stripe_customer_id = db.Column(db.String(128))

    def set_password(self, password: str):
        self.password_hash = bcrypt.hash(password)

    def check_password(self, password: str) -> bool:
        return bcrypt.verify(password, self.password_hash)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    provider = db.Column(db.String(20), nullable=False)          # 'stripe' | 'paypal'
    provider_order_id = db.Column(db.String(128), unique=True, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(32), nullable=False)             # 'completed' | 'deposit' | 'canceled' ...
    email = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

with app.app_context():
    db.create_all()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# =======================
#   UTILITAIRES
# =======================
def send_email(to_email, subject, body):
    if not SMTP_HOST or not to_email:
        return False
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr(('PUBAJIM', SMTP_FROM))
    msg['To'] = to_email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to_email] + ([ADMIN_EMAIL] if ADMIN_EMAIL else []), msg.as_string())
        return True
    except Exception as e:
        print('EMAIL ERROR:', e)
        return False

def record_order(provider, provider_order_id, amount_cents, currency, status, email=None):
    order = Order.query.filter_by(provider_order_id=provider_order_id).first()
    if not order:
        order = Order(provider=provider, provider_order_id=provider_order_id,
                      amount_cents=amount_cents, currency=currency, status=status, email=email)
        db.session.add(order)
    else:
        order.status = status
        if email and not order.email:
            order.email = email
    db.session.commit()
    return order

# =======================
#   PAGES DU SITE
# =======================
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/produit')
def product():
    return render_template(
        'product.html',
        product=PRODUCT,
        stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
        paypal_client_id=PAYPAL_CLIENT_ID,
        paypal_env=PAYPAL_ENV
    )

@app.route('/comment-ca-marche')
def explain():
    return render_template('explain.html')

@app.route('/offres')
def offres():
    return render_template('offres.html')

@app.route('/tarifs')
def tarifs():
    return render_template('tarifs.html')

@app.route('/temoignages')
def temoignages():
    return render_template('temoignages.html')

@app.route('/a-propos')
def about():
    return render_template('about.html')

@app.route('/contact', methods=['GET','POST'])
def contact():
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        email = request.form.get('email','').strip()
        subject = request.form.get('subject','').strip() or 'Contact PUBAJIMJIM'
        message = request.form.get('message','').strip()
        body = f"Sujet: {subject}\nDe: {name} <{email}>\n\n{message}"
        if email:
            send_email(email, 'PUBAJIM — nous avons bien reçu votre message', 'Merci, nous revenons vers vous rapidement.')
        if ADMIN_EMAIL:
            send_email(ADMIN_EMAIL, 'Nouveau message — PUBAJIMJIM', body)
        return render_template('contact_success.html')
    subject = request.args.get('subject','')
    message = request.args.get('message','')
    return render_template('contact.html', subject=subject, message=message)

@app.route('/mentions-legales')
def mentions():
    return render_template('mentions.html')

@app.route('/cgv')
def cgv():
    return render_template('cgv.html')

@app.route('/confidentialite')
def privacy():
    return render_template('privacy.html')

@app.route('/raspberry')
def raspberry():
    photos = [
        {'file': 'androidtv15.jpg', 'fallback': 'hw-display.svg', 'caption': "Android TV + lecteur (exemple d'affichage)"},
        {'file': 'rpi-kit.jpg', 'fallback': 'hw-board.svg', 'caption': "Kit Raspberry Pi complet (carte, micro-SD, câbles, alim, ventilateur)"},
        {'file': 'rpi-logo.png', 'fallback': 'hw-board.svg', 'caption': "Logo Raspberry Pi (illustration)"},
        {'file': 'rpi-case.jpg', 'fallback': 'hw-case.svg', 'caption': "Boîtier ventilé pour Raspberry Pi"},
        {'file': 'tv-sizes.jpg', 'fallback': 'hw-display.svg', 'caption': "Exemples de tailles d’écrans (32/42/52/65 pouces)"}
    ]
    return render_template('raspberry.html', photos=photos, stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/ecrans')
def ecrans():
    tvs = [
        {'size':32,'label':'Écran 32"','dimensions_cm':'71 × 40','image':'tv-32.svg','price_buy':'149','price_sub':'9'},
        {'size':43,'label':'Écran 43"','dimensions_cm':'95 × 53','image':'tv-43.svg','price_buy':'229','price_sub':'12'},
        {'size':55,'label':'Écran 55"','dimensions_cm':'122 × 69','image':'tv-55.svg','price_buy':'349','price_sub':'19'},
        {'size':65,'label':'Écran 65"','dimensions_cm':'144 × 81','image':'tv-65.svg','price_buy':'499','price_sub':'25'},
    ]
    return render_template('ecrans.html', tvs=tvs, stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

# =======================
#   STRIPE — ACHAT UNIQUE
# =======================
@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=[{
                'price_data': {
                    'currency': PRODUCT['currency'],
                    'product_data': {
                        'name': PRODUCT['name'],
                        'description': PRODUCT['description']
                    },
                    'unit_amount': PRODUCT['amount_cents']
                },
                'quantity': 1
            }],
            success_url=request.url_root + 'success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.url_root + 'cancel',
            customer_creation='if_required',
        )
        return jsonify({'id': session.id})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# =======================
#   STRIPE — ACOMPTE + ABONNEMENT
# =======================
@app.route('/create-deposit-checkout', methods=['POST'])
def create_deposit_checkout():
    try:
        data = request.get_json(silent=True) or {}
        qty = int(data.get('qty', 1))
        if qty < 1: qty = 1
        if qty > 50: qty = 50
        note = str(data.get('note', 'Acompte abonnement'))

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': { 'name': f"Acompte PUBAJIMJIM ({qty} écran(s))", 'description': note },
                    'unit_amount': 20000  # 200 €
                },
                'quantity': qty
            }],
            metadata={
                'is_deposit': '1',
                'qty': str(qty),
                'price_id': STRIPE_PRICE_SUB_ID or '',
                'note': note
            },
            success_url=request.url_root + 'success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.url_root + 'cancel',
            customer_creation='always',
        )
        return jsonify({'id': session.id})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# =======================
#   PAGES SUCCÈS / ANNULATION
# =======================
@app.route('/success')
def success():
    return "<h2>Paiement réussi — merci !</h2><p>Un e-mail de confirmation vous sera envoyé.</p><p>Retour à la <a href='%s'>boutique</a>.</p>" % url_for('product')

@app.route('/cancel')
def cancel():
    return "<h2>Paiement annulé.</h2><p>Retour à la <a href='%s'>boutique</a>.</p>" % url_for('product')

# =======================
#   STRIPE — WEBHOOK
# =======================
@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if event['type'] == 'checkout.session.completed':
        s = event['data']['object']

        email = (s.get('customer_details') or {}).get('email')
        amount = int(s.get('amount_total') or PRODUCT['amount_cents'])
        currency = (s.get('currency') or PRODUCT['currency'])
        order_id = s.get('id')
        metadata = s.get('metadata') or {}
        is_deposit = metadata.get('is_deposit') == '1'
        qty = int((metadata.get('qty') or '1') or 1)
        price_id = metadata.get('price_id') or STRIPE_PRICE_SUB_ID or ''

        status = 'deposit' if is_deposit else 'completed'
        order = record_order('stripe', order_id, amount, currency, status, email)

        # Lier le customer Stripe au compte utilisateur si même email
        try:
            if email and s.get('customer'):
                u = User.query.filter_by(email=email.lower()).first()
                if u and not u.stripe_customer_id:
                    u.stripe_customer_id = s.get('customer')
                    db.session.commit()
        except Exception as _e:
            print('USER LINK ERROR:', _e)

        # Emails
        if email:
            if status == 'deposit':
                send_email(
                    email,
                    'Acompte reçu — PUBAJIMJIMJIM',
                    f"Bonjour,\n\nNous avons bien reçu votre acompte de {amount/100:.2f} {currency.upper()} "
                    f"pour {qty} écran(s). Nous allons activer votre abonnement.\n\n"
                    f"Référence: {order.provider_order_id}\nDate: {order.created_at} UTC\n\nCordialement,\nPUBA"
                )
            else:
                send_email(
                    email,
                    'Confirmation de paiement — PUBAJIMJIMJIM',
                    f"Bonjour,\n\nMerci pour votre achat: {PRODUCT['name']} ({amount/100:.2f} {currency.upper()}).\n\n"
                    f"Référence: {order.provider_order_id}\nDate: {order.created_at} UTC\n\nCordialement,\nPUBA"
                )
        if ADMIN_EMAIL:
            send_email(
                ADMIN_EMAIL,
                f"[PUBAJIM] Paiement {'ACOMPTE' if is_deposit else 'ACHAT'} — {order.provider_order_id}",
                json.dumps({'provider': 'stripe', 'order_id': order.provider_order_id, 'email': email, 'amount_cents': amount,
                            'currency': currency, 'is_deposit': is_deposit, 'qty': qty}, indent=2)
            )

        # Si acompte => créer automatiquement l'abonnement
        try:
            if is_deposit and price_id and s.get('customer'):
                sub = stripe.Subscription.create(
                    customer=s.get('customer'),
                    items=[{'price': price_id, 'quantity': qty}],
                    payment_behavior='default_incomplete',
                    expand=['latest_invoice.payment_intent']
                )
                if ADMIN_EMAIL:
                    send_email(
                        ADMIN_EMAIL,
                        f"[PUBAJIM] Abonnement créé (post-acompte)",
                        json.dumps({'subscription_id': sub.id, 'customer': s.get('customer'),
                                    'price_id': price_id, 'qty': qty}, indent=2)
                    )
        except Exception as e:
            print("SUBSCRIPTION ERROR:", e)

    return jsonify({'received': True})

# =======================
#   PAYPAL (optionnel)
# =======================
def get_paypal_access_token():
    auth = (PAYPAL_CLIENT_ID, PAYPAL_SECRET)
    headers = {'Accept': 'application/json', 'Accept-Language': 'en_US'}
    data = {'grant_type': 'client_credentials'}
    r = requests.post(PAYPAL_OAUTH_URL, auth=auth, data=data, headers=headers)
    r.raise_for_status()
    return r.json()['access_token']

@app.route('/create-paypal-order', methods=['POST'])
def create_paypal_order():
    try:
        token = get_paypal_access_token()
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
        payload = {
            'intent': 'CAPTURE',
            'purchase_units': [{
                'amount': {
                    'currency_code': PRODUCT['currency'].upper(),
                    'value': f"{PRODUCT['amount_cents']/100:.2f}"
                },
                'description': PRODUCT['description']
            }]
        }
        r = requests.post(PAYPAL_ORDERS_URL, json=payload, headers=headers)
        r.raise_for_status()
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/capture-paypal-order/<order_id>', methods=['POST'])
def capture_paypal_order(order_id):
    try:
        token = get_paypal_access_token()
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'}
        r = requests.post(f"{PAYPAL_ORDERS_URL}/{order_id}/capture", headers=headers)
        r.raise_for_status()
        data = r.json()
        status = data.get('status') or 'COMPLETED'
        payer_email = (data.get('payer') or {}).get('email_address')
        order = record_order('paypal', order_id, PRODUCT['amount_cents'], PRODUCT['currency'], status.lower(), payer_email)
        if payer_email:
            send_email(
                payer_email,
                'Confirmation de paiement — PUBAJIMJIMJIM',
                f"Bonjour,\n\nMerci pour votre achat : {PRODUCT['name']} ({PRODUCT['amount_cents']/100:.2f} {PRODUCT['currency'].upper()}).\n\n"
                f"Référence: {order.provider_order_id}\nDate: {order.created_at} UTC\n\nCordialement,\nPUBA"
            )
        if ADMIN_EMAIL:
            send_email(
                ADMIN_EMAIL,
                f"[PUBAJIM] Paiement PayPal {order.status} — {order.provider_order_id}",
                json.dumps({'provider': 'paypal', 'order_id': order.provider_order_id, 'email': payer_email}, indent=2)
            )
        return jsonify({'ok': True, 'status': status})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

# =======================
#   AUTH (signup/login/logout)
# =======================
@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        if not email or not password:
            flash('Email et mot de passe requis.', 'error')
            return redirect(url_for('signup'))
        if User.query.filter_by(email=email).first():
            flash('Un compte existe déjà avec cet email.', 'error')
            return redirect(url_for('signup'))
        u = User(email=email)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        login_user(u)
        flash('Bienvenue ! Votre compte est créé.', 'success')
        return redirect(url_for('home'))
    return render_template('signup.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        u = User.query.filter_by(email=email).first()
        if not u or not u.check_password(password):
            flash('Identifiants invalides.', 'error')
            return redirect(url_for('login'))
        login_user(u)
        flash('Connexion réussie.', 'success')
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Déconnecté.', 'success')
    return redirect(url_for('home'))

# =======================
#   RUN
# =======================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
