import os
import json
import time
import random
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- App Configuration ---
app = Flask(__name__)
# Set a strong secret key for security
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_strong_fallback_secret_key')
# Use an in-memory SQLite database for a fast prototype
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
# Use a message queue for production, but for a prototype, we can keep it simple
socketio = SocketIO(app, cors_allowed_origins="*")

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')

# Initialize Twilio client
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

# --- Mock Data for a Hackathon ---
# Mock MSP (Minimum Support Price) per crop in Rs./quintal
MSP_PRICES = {
    'wheat': 2275,
    'rice': 2203,
    'maize': 2090,
}

# Mock historical prices per district
HISTORICAL_PRICES = {
    'mumbai': {'wheat': [2300, 2350, 2400], 'rice': [2500, 2550, 2600]},
    'delhi': {'wheat': [2250, 2300, 2320], 'rice': [2450, 2480, 2510]},
}

# --- Database Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone_number = db.Column(db.String(20), unique=True, nullable=False)
    user_type = db.Column(db.String(20), nullable=False) # 'farmer' or 'buyer'
    # For MSP alerts and contracts, we'll need to know their district
    district = db.Column(db.String(50), nullable=True)
    login_code = db.Column(db.String(12), unique=True, nullable=True)

class Lot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    farmer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    crop_type = db.Column(db.String(50), nullable=False)
    quantity_kg = db.Column(db.Float, nullable=False)
    min_price = db.Column(db.Float, nullable=True) # MSP or user-defined
    status = db.Column(db.String(20), default='open') # open, closed, sold
    is_collective = db.Column(db.Boolean, default=False)
    members = db.Column(db.Text, default="[]") # Store a list of farmer_ids as JSON string
    
class Bid(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey('lot.id'), nullable=False)
    bidder_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    bid_amount = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: time.time())

# --- Core Business Logic ---

def get_or_create_user(phone_number, user_type, district=None):
    """Finds or creates a user based on phone number."""
    user = User.query.filter_by(phone_number=phone_number).first()
    if not user:
        # Generate a simple 12-digit login code for new users
        login_code = ''.join([str(random.randint(0, 9)) for _ in range(12)])
        user = User(phone_number=phone_number, user_type=user_type, district=district, login_code=login_code)
        db.session.add(user)
        db.session.commit()
        # Send the code to the user (in a real app, this would be a one-time thing)
        send_sms(phone_number, f"Welcome to Farmer Market! Your login code is {login_code}. Use this for the web portal.")
    return user

def send_sms(to, message):
    """Sends an SMS message using the Twilio client."""
    if not client or not TWILIO_PHONE_NUMBER:
        print(f"SMS not sent (Twilio not configured): To: {to}, Msg: {message}")
        return
    
    try:
        client.messages.create(
            to=to,
            from_=TWILIO_PHONE_NUMBER,
            body=message
        )
        print(f"SMS sent successfully to {to}")
    except Exception as e:
        print(f"Error sending SMS to {to}: {e}")

# --- API Endpoints ---
@app.route('/')
def index():
    return "Farmer Marketplace Backend is running!"

# Buyer Dashboard API
@app.route('/api/v1/lots', methods=['GET'])
def get_all_lots():
    lots = Lot.query.filter_by(status='open').all()
    all_lots_data = []
    for lot in lots:
        highest_bid = db.session.query(db.func.max(Bid.bid_amount)).filter_by(lot_id=lot.id).scalar() or 0
        
        all_lots_data.append({
            'id': lot.id,
            'crop_type': lot.crop_type,
            'quantity_kg': lot.quantity_kg,
            'min_price': lot.min_price,
            'highest_bid': highest_bid
        })
        
    return jsonify(all_lots_data)

# SMS/WhatsApp Webhook Endpoint for non-internet users
@app.route("/sms", methods=['POST'])
def sms_handler():
    response = MessagingResponse()
    body = request.values.get('Body', '').strip().lower()
    phone_number = request.values.get('From', '')

    parts = body.split(' ')
    command = parts[0]

    if command == 'list' and len(parts) >= 3:
        # Command: LIST <CROP_TYPE> <QUANTITY> [<MIN_PRICE>]
        # e.g., "LIST WHEAT 500", or "LIST WHEAT 500 2300"
        crop_type = parts[1]
        try:
            quantity = float(parts[2])
            min_price = float(parts[3]) if len(parts) > 3 else MSP_PRICES.get(crop_type, None)
            
            user = get_or_create_user(phone_number, 'farmer')
            
            new_lot = Lot(farmer_id=user.id, crop_type=crop_type, quantity_kg=quantity, min_price=min_price)
            db.session.add(new_lot)
            db.session.commit()
            
            price_msg = f" at your minimum price of Rs. {min_price}" if min_price else " at the current MSP"
            response.message(f"Lot listed! Crop: {crop_type}, Quantity: {quantity}kg. Lot ID: {new_lot.id}{price_msg}.")
        except (ValueError, IndexError):
            response.message("Invalid format. Use 'LIST CROP QUANTITY [PRICE]'.")

    elif command == 'bid' and len(parts) == 3:
        # Command: BID <LOT_ID> <AMOUNT>
        # e.g., "BID 123 2500"
        try:
            lot_id = int(parts[1])
            bid_amount = float(parts[2])
            
            user = get_or_create_user(phone_number, 'buyer')
            
            # Check if lot is valid and open
            lot = Lot.query.get(lot_id)
            if not lot or lot.status != 'open':
                response.message("Lot is not available for bidding.")
                return str(response)

            # Check if bid meets minimum price
            if lot.min_price and bid_amount < lot.min_price:
                response.message(f"Your bid is too low. Minimum price is Rs. {lot.min_price}.")
                return str(response)

            new_bid = Bid(lot_id=lot_id, bidder_id=user.id, bid_amount=bid_amount)
            db.session.add(new_bid)
            db.session.commit()
            
            # Use SocketIO to alert the web dashboard of the new bid
            socketio.emit('bid_update', {'lot_id': lot_id, 'bid_amount': bid_amount})

            response.message(f"Your bid of Rs. {bid_amount} for lot {lot_id} has been recorded.")
            
            # MSP Alert for the farmer
            msp = MSP_PRICES.get(lot.crop_type.lower())
            if msp and bid_amount < msp:
                farmer = User.query.get(lot.farmer_id)
                if farmer and farmer.phone_number:
                    send_sms(farmer.phone_number, 
                             f"ALERT: A bid of Rs. {bid_amount} on your {lot.crop_type} lot {lot_id} is below the MSP of Rs. {msp}.")

        except (ValueError, IndexError):
            response.message("Invalid format. Use 'BID LOT_ID AMOUNT'.")
    
    elif command == 'collective' and len(parts) >= 3:
        # Command: COLLECTIVE <CROP_TYPE> <QUANTITY> <DISTRICT>
        # e.g., "COLLECTIVE WHEAT 100 DELHI"
        crop_type = parts[1]
        try:
            quantity = float(parts[2])
            district = parts[3]
            
            user = get_or_create_user(phone_number, 'farmer', district=district)
            
            # Check for an existing collective lot for the same crop and district
            collective_lot = Lot.query.filter_by(
                is_collective=True,
                crop_type=crop_type,
                status='open'
            ).first()
            
            if collective_lot:
                members = json.loads(collective_lot.members)
                members.append(user.id)
                collective_lot.members = json.dumps(members)
                collective_lot.quantity_kg += quantity
                db.session.commit()
                response.message(f"You have joined the collective lot for {crop_type}. Total quantity is now {collective_lot.quantity_kg}kg. Collective Lot ID: {collective_lot.id}")
            else:
                members = [user.id]
                new_collective_lot = Lot(
                    farmer_id=user.id, 
                    crop_type=crop_type, 
                    quantity_kg=quantity, 
                    is_collective=True,
                    members=json.dumps(members)
                )
                db.session.add(new_collective_lot)
                db.session.commit()
                response.message(f"New collective lot for {crop_type} created. Lot ID: {new_collective_lot.id}.")
        except (ValueError, IndexError):
            response.message("Invalid format. Use 'COLLECTIVE CROP QUANTITY DISTRICT'.")

    else:
        response.message("Welcome to the marketplace! Commands: 'LIST <CROP> <QTY> [PRICE]' or 'BID <LOT_ID> <AMT>'.")

    return str(response)

# --- Historical Data and Contract Generation (for demo purposes) ---
@app.route('/api/v1/historical_prices/<string:crop_type>/<string:district>', methods=['GET'])
def get_historical_prices(crop_type, district):
    prices = HISTORICAL_PRICES.get(district.lower(), {}).get(crop_type.lower())
    if prices:
        return jsonify({'historical_prices': prices})
    return jsonify({'message': 'No historical data available for this crop and district.'}), 404

@app.route('/api/v1/generate_contract/<int:lot_id>', methods=['GET'])
def generate_contract(lot_id):
    lot = Lot.query.get_or_404(lot_id)
    highest_bid = db.session.query(db.func.max(Bid.bid_amount)).filter_by(lot_id=lot.id).scalar() or 0
    buyer_bid = Bid.query.filter_by(lot_id=lot.id, bid_amount=highest_bid).first()
    
    if not buyer_bid:
        return jsonify({'message': 'No bids on this lot yet.'}), 404

    farmer = User.query.get(lot.farmer_id)
    buyer = User.query.get(buyer_bid.bidder_id)

    contract_template = f"""
    --- CONTRACT FOR ADVANCE SALE ---
    
    This contract is for the advance sale of agricultural produce.
    
    1. Seller (Farmer): {farmer.phone_number}
    2. Buyer: {buyer.phone_number}
    
    3. Produce Details:
        - Crop Type: {lot.crop_type}
        - Quantity: {lot.quantity_kg} kg
    
    4. Price:
        - Agreed Rate: Rs. {highest_bid} per kg
        - Total Amount: Rs. {highest_bid * lot.quantity_kg}
    
    5. Terms:
        - Payment to be made upon delivery.
        - Quality to be verified upon arrival.
    
    This is a binding agreement.
    """
    return jsonify({'contract': contract_template})


# --- NEW: Sale Finalization Endpoint ---
@app.route('/api/v1/confirm_sale/<int:lot_id>', methods=['POST'])
def confirm_sale(lot_id):
    lot = Lot.query.get_or_404(lot_id)
    highest_bid = db.session.query(db.func.max(Bid.bid_amount)).filter_by(lot_id=lot.id).scalar() or 0

    if not highest_bid:
        return jsonify({'message': 'No bids to confirm sale for this lot.'}), 400
    
    # Update lot status
    lot.status = 'sold'
    db.session.commit()

    buyer = Bid.query.filter_by(lot_id=lot_id, bid_amount=highest_bid).first().bidder
    farmer = lot.farmer

    # Send notifications to both parties
    send_sms(farmer.phone_number, f"CONGRATS! Your {lot.crop_type} lot {lot.id} has been sold for Rs. {highest_bid} per kg.")
    send_sms(buyer.phone_number, f"SUCCESS! Your bid of Rs. {highest_bid} per kg for lot {lot.id} has won the auction. The lot is now closed.")

    return jsonify({'message': 'Sale confirmed and notifications sent.'})

# --- NEW: Login Endpoint for Web Portal ---
@app.route('/api/v1/login', methods=['POST'])
def login():
    data = request.get_json()
    phone_number = data.get('phone_number')
    login_code = data.get('login_code')

    user = User.query.filter_by(phone_number=phone_number, login_code=login_code).first()

    if user:
        return jsonify({'message': 'Login successful', 'user_id': user.id, 'user_type': user.user_type}), 200
    else:
        return jsonify({'message': 'Invalid credentials'}), 401


if __name__ == '__main__':
    # With app_context, we can create the database before running the server
    with app.app_context():
        db.create_all()
    
    # Run the app. In a hackathon, this is fine. For production, you'd use a WSGI server like Gunicorn.
    
    socketio.run(app, debug=True, port=5000, allow_unsafe_werkzeug=True)

