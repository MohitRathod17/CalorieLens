from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from groq import Groq
import os
from datetime import datetime
from dotenv import load_dotenv
import json
import requests 
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "calorie-tracker-secret-key-123")

# Initialize Groq client
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# MongoDB Configurations
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/calories")
try:
    mongo_client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000,  # 5 second timeout
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
    )
    # Verify connection is actually alive at startup
    mongo_client.admin.command('ping')
    print("[MongoDB] Connected successfully.")
    db = mongo_client.get_database("calories")
except Exception as e:
    print(f"[MongoDB] WARNING: Could not connect at startup: {e}")
    mongo_client = None
    db = None

def get_next_id(collection_name):
    counter = db.counters.find_one_and_update(
        {"_id": collection_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return counter["seq"]

def parse_params(params):
    filter_dict = {}
    if not params:
        return filter_dict
    for k, v in params.items():
        field_name = "_id" if k == "id" else k
        if isinstance(v, str) and v.startswith("eq."):
            val = v[3:]
            if val.isdigit():
                filter_dict[field_name] = int(val)
            else:
                filter_dict[field_name] = val
        else:
            filter_dict[field_name] = int(v) if (isinstance(v, str) and v.isdigit()) else v
    return filter_dict

def query_supabase(table, method="GET", data=None, params=None):
    """
    Local MongoDB replacement for query_supabase to store all logs, 
    users, and companion card assets.
    """
    global db, mongo_client
    if db is None:
        try:
            mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
            )
            mongo_client.admin.command('ping')
            db = mongo_client.get_database("calories")
            print("[MongoDB] Reconnected successfully.")
        except Exception as e:
            print(f"[MongoDB] Reconnect failed: {e}")
            raise RuntimeError(f"Database unavailable: {e}")

    collection = db[table]
    filter_dict = parse_params(params)
    
    if method == "GET":
        results = list(collection.find(filter_dict))
        for r in results:
            r['id'] = r.get('_id')
        return results
        
    elif method == "POST":
        if not data:
            return None
        doc = data.copy()
        if '_id' not in doc and 'id' not in doc:
            doc['_id'] = get_next_id(table)
            doc['id'] = doc['_id']
        elif 'id' in doc:
            doc['_id'] = doc['id']
            
        collection.insert_one(doc)
        doc['id'] = doc['_id']
        return [doc]
        
    elif method == "PATCH":
        if not data:
            return None
        collection.update_many(filter_dict, {"$set": data})
        results = list(collection.find(filter_dict))
        for r in results:
            r['id'] = r.get('_id')
        return results
        
    elif method == "DELETE":
        collection.delete_many(filter_dict)
        return []
        
    return None

def get_calories_from_groq(query, prep_type):
    """Use Groq API to estimate calories and macros for a list of food items."""
    try:
        system_prompt = (
            f"You are a professional nutrition expert. The user will provide a list of food items, one or more, "
            f"which were prepared as {prep_type} style. Estimate the calories and macronutrients for each item "
            f"individually, taking into account that {prep_type} preparation affects calorie and fat density "
            f"(e.g., restaurant food generally has more fat, butter, oil, sugar, and calories than homemade food). "
            f"Return the output as a valid JSON array of objects, where each object has keys: "
            f"'name' (string), 'calories' (integer), 'protein' (integer, grams), 'carbs' (integer, grams), 'fat' (integer, grams). "
            f"Do not include any explanation, markdown block formatting, or other text outside the JSON array. "
            f"Example: "
            f'[{{"name": "paneer tikka masala 100g", "calories": 250, "protein": 14, "carbs": 8, "fat": 18}}, '
            f'{{"name": "ghee roti 2", "calories": 180, "protein": 4, "carbs": 30, "fat": 5}}]'
        )
        
        chat_completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=600,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query}
            ]
        )
        
        response_text = chat_completion.choices[0].message.content.strip()
        
        # Clean potential markdown wrapping
        if response_text.startswith("```"):
            lines = response_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            response_text = "\n".join(lines).strip()
            
        return json.loads(response_text)
    except Exception as e:
        print(f"Error calling Groq API: {e}")
        return None

# ================= AUTHENTICATION ROUTES =================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get("user_id"):
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return render_template('login.html', error="Please enter username and password.")
        
        try:
            # Query MongoDB for user
            users = query_supabase('users', 'GET', params={'username': f'eq.{username}'})
        except Exception as e:
            print(f"[Login] DB error: {e}")
            return render_template('login.html', error="Database connection error. Please try again in a moment.")
        
        if users and len(users) > 0:
            user = users[0]
            if check_password_hash(user['password_hash'], password):
                session['user_id'] = user['id']
                session['username'] = user['username']
                return redirect(url_for('index'))
                
        return render_template('login.html', error="Invalid username or password.")
        
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if session.get("user_id"):
        return redirect(url_for('index'))
        
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip()
        
        if not username or not password or not name:
            return render_template('signup.html', error="All fields are required.")
            
        try:
            # Check if user already exists
            existing = query_supabase('users', 'GET', params={'username': f'eq.{username}'})
        except Exception as e:
            print(f"[Signup] DB error: {e}")
            return render_template('signup.html', error="Database connection error. Please try again in a moment.")

        if existing and len(existing) > 0:
            return render_template('signup.html', error="Username already exists.")
            
        # Hash password and insert
        pw_hash = generate_password_hash(password)
        new_user = {
            'username': username,
            'password_hash': pw_hash,
            'name': name,
            'age': 25,          # default placeholders
            'weight': 70.0,
            'height': 170.0
        }
        
        try:
            created = query_supabase('users', 'POST', data=new_user)
        except Exception as e:
            print(f"[Signup] DB insert error: {e}")
            return render_template('signup.html', error="Database connection error. Please try again in a moment.")

        if created and len(created) > 0:
            session['user_id'] = created[0]['id']
            session['username'] = created[0]['username']
            return redirect(url_for('index'))
            
        return render_template('signup.html', error="Registration failed. Please try again.")
        
    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ================= DASHBOARD & LOGS =================

@app.route('/')
def index():
    """Render dashboard if logged in"""
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for('login'))
        
    # Get user profile details
    users = query_supabase('users', 'GET', params={'id': f'eq.{user_id}'})
    if not users:
        session.clear()
        return redirect(url_for('login'))
    user = users[0]
    
    # Calculate BMI
    bmi = 0
    bmi_status = "Unknown"
    weight = user.get('weight')
    height = user.get('height')
    if weight and height and height > 0:
        height_meters = height / 100.0
        bmi = round(weight / (height_meters ** 2), 1)
        if bmi < 18.5:
            bmi_status = "Underweight"
        elif bmi < 24.9:
            bmi_status = "Normal Weight"
        elif bmi < 29.9:
            bmi_status = "Overweight"
        else:
            bmi_status = "Obese"
            
    # Fetch logs from Supabase
    logs = query_supabase('logs', 'GET', params={'user_id': f'eq.{user_id}'}) or []
    
    consumed_items = [l for l in logs if l['type'] == 'consumed']
    burned_items = [l for l in logs if l['type'] == 'burned']
    
    total_consumed = sum(item['calories'] for item in consumed_items)
    total_burned = sum(item['calories'] for item in burned_items)
    net_calories = total_consumed - total_burned

    # Today-specific calories
    from datetime import date
    today_str = date.today().isoformat()
    today_consumed = sum(item['calories'] for item in consumed_items if item.get('date') == today_str)
    today_burned   = sum(item['calories'] for item in burned_items   if item.get('date') == today_str)

    # Today's macros
    today_items = [i for i in consumed_items if i.get('date') == today_str]
    today_protein = sum(int(i.get('protein') or 0) for i in today_items)
    today_carbs   = sum(int(i.get('carbs') or 0)   for i in today_items)
    today_fat     = sum(int(i.get('fat') or 0)     for i in today_items)

    # Estimate BMR (Mifflin-St Jeor, default male)
    age    = user.get('age') or 25
    w      = user.get('weight') or 70.0
    h      = user.get('height') or 170.0
    bmr    = round(10 * w + 6.25 * h - 5 * age + 5)  # male formula
    
    return render_template(
        'dashboard.html',
        user=user,
        bmi=bmi,
        bmi_status=bmi_status,
        total_consumed=total_consumed,
        total_burned=total_burned,
        net_calories=net_calories,
        consumed_items=consumed_items,
        burned_items=burned_items,
        today_consumed=today_consumed,
        today_burned=today_burned,
        today_protein=today_protein,
        today_carbs=today_carbs,
        today_fat=today_fat,
        bmr=bmr
    )

@app.route('/api/profile', methods=['POST'])
def update_profile():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    name = data.get('name', '').strip()
    age = data.get('age')
    weight = data.get('weight')
    height = data.get('height')
    
    if not name:
        return jsonify({'error': 'Name is required'}), 400
        
    try:
        update_data = {
            'name': name,
            'age': int(age) if age else 25,
            'weight': float(weight) if weight else 70.0,
            'height': float(height) if height else 170.0
        }
    except ValueError:
        return jsonify({'error': 'Invalid numeric values for age, weight, or height.'}), 400
        
    res = query_supabase('users', 'PATCH', data=update_data, params={'id': f'eq.{user_id}'})
    if res is not None:
        return jsonify({'success': True, 'profile': update_data})
    return jsonify({'error': 'Failed to update profile.'}), 500

@app.route('/api/add-consumed', methods=['POST'])
def add_consumed():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    query = data.get('query', '').strip()
    prep_type = data.get('prep_type', 'Homemade').strip()
    meal_type = data.get('meal_type', 'Breakfast').strip()
    date_str = data.get('date', datetime.now().strftime('%Y-%m-%d')).strip()
    
    if not query:
        return jsonify({'error': 'Food items list is required'}), 400
    
    # Get items from Groq
    items = get_calories_from_groq(query, prep_type)
    
    if not items or not isinstance(items, list):
        return jsonify({'error': 'Could not parse or estimate calories for the food items. Check your inputs or try again.'}), 400
    
    added_items = []
    total_added_calories = 0
    for item_data in items:
        name = item_data.get('name', '').strip()
        calories = item_data.get('calories')
        try:
            calories = int(calories)
        except (ValueError, TypeError):
            continue
            
        if not name or calories <= 0:
            continue
            
        log_entry = {
            'user_id': user_id,
            'type': 'consumed',
            'name': f"{name} ({prep_type})",
            'calories': calories,
            'protein': int(item_data.get('protein') or 0),
            'carbs': int(item_data.get('carbs') or 0),
            'fat': int(item_data.get('fat') or 0),
            'date': date_str,
            'meal_type': meal_type,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        }
        
        db_res = query_supabase('logs', 'POST', data=log_entry)
        if db_res and len(db_res) > 0:
            added_items.append(db_res[0])
            total_added_calories += calories
            
    if not added_items:
        return jsonify({'error': 'No valid estimated items could be generated.'}), 400
        
    return jsonify({
        'success': True,
        'calories': total_added_calories,
        'items': added_items
    })

@app.route('/api/add-burned', methods=['POST'])
def add_burned():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    activity = data.get('activity', '').strip()
    calories_raw = data.get('calories', 0)
    try:
        calories = int(calories_raw)
    except (ValueError, TypeError):
        return jsonify({'error': 'Calories must be a numeric value'}), 400
    date_str = data.get('date', datetime.now().strftime('%Y-%m-%d')).strip()
    
    if not activity or calories <= 0:
        return jsonify({'error': 'Activity and calories are required'}), 400
    
    log_entry = {
        'user_id': user_id,
        'type': 'burned',
        'name': activity,
        'calories': int(calories),
        'date': date_str,
        'meal_type': 'Exercise',
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }
    
    db_res = query_supabase('logs', 'POST', data=log_entry)
    if db_res and len(db_res) > 0:
        return jsonify({'success': True, 'item': db_res[0]})
    return jsonify({'error': 'Failed to save burned log.'}), 500

@app.route('/api/delete-log/<int:log_id>', methods=['DELETE'])
def delete_log(log_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    res = query_supabase('logs', 'DELETE', params={'id': f'eq.{log_id}', 'user_id': f'eq.{user_id}'})
    if res is not None:
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete log entry.'}), 500

@app.route('/api/clear', methods=['POST'])
def clear_data():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    res = query_supabase('logs', 'DELETE', params={'user_id': f'eq.{user_id}'})
    if res is not None:
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to clear log data.'}), 500

# ================= VLM BODY FAT TIMELINE =================

@app.route('/api/analyze-body-fat', methods=['POST'])
def analyze_body_fat():
    """Analyze physique photo via OpenRouter VLM and calculate target timeline"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    image_b64 = data.get('image')
    weight = data.get('weight')
    target_bf = data.get('target_bf')
    
    if not image_b64 or not weight or not target_bf:
        return jsonify({'error': 'Image, weight, and target body fat percentage are required.'}), 400
        
    try:
        weight = float(weight)
        target_bf = float(target_bf)
    except ValueError:
        return jsonify({'error': 'Weight and target body fat % must be numeric values.'}), 400

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return jsonify({'error': 'OPENROUTER_API_KEY environment variable is not set. Please add it to your .env file.'}), 400

    # Calculate average daily calories burned from Supabase logs
    logs = query_supabase('logs', 'GET', params={'user_id': f'eq.{user_id}'}) or []
    burned_by_date = {}
    for log in logs:
        if log['type'] == 'burned':
            d = log['date']
            burned_by_date[d] = burned_by_date.get(d, 0) + log['calories']
            
    avg_burned_per_day = sum(burned_by_date.values()) / len(burned_by_date) if len(burned_by_date) > 0 else 300.0

    try:
        # call OpenRouter VLM
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5000",
            "X-Title": "Calorie Tracker"
        }
        
        system_prompt = (
            "You are an expert fitness evaluator and sports scientist. Analyze the uploaded physique photo "
            "and estimate the person's body fat percentage as accurately and objectively as possible. "
            "Follow these guidelines:\n"
            "- 5-9%: Extremely shredded, striations visible, full abdominal definition, vascular.\n"
            "- 10-14%: Athletic/lean, clear ab definition, visible obliques, low fat.\n"
            "- 15-19%: Fit, outline of abs visible under good lighting, minimal definition elsewhere.\n"
            "- 20-24%: Average, soft definition, no visible abs, normal waistline.\n"
            "- 25-29%: Excess body fat, soft muscle outline, slight stomach protrusion.\n"
            "- 30%+: Obese, significant abdominal fat storage.\n\n"
            "Estimate the body fat % and reply with ONLY a single float or integer number representing the "
            "body fat percentage (e.g., '14.5' or '22'). Do not include the '%' symbol, any introductory text, "
            "or explanations."
        )
        
        payload = {
            "model": "nvidia/nemotron-nano-12b-v2-vl:free",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": system_prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_b64,
                                "detail": "high"
                            }
                        }
                    ]
                }
            ]
        }
        
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        response_json = response.json()
        
        if response.status_code != 200 or 'choices' not in response_json:
            error_msg = response_json.get('error', {}).get('message', 'Unknown OpenRouter error')
            return jsonify({'error': f"OpenRouter API error: {error_msg}"}), 500
            
        result_text = response_json['choices'][0]['message']['content'].strip()
        
        # Extract the estimated BF number
        import re
        match = re.search(r'\d+(\.\d+)?', result_text)
        if not match:
            return jsonify({'error': f"Failed to parse estimated body fat percentage from response: '{result_text}'"}), 500
            
        estimated_bf = float(match.group())
        
        if estimated_bf <= target_bf:
            return jsonify({
                'success': True,
                'estimated_bf': estimated_bf,
                'days_needed': 0,
                'fat_to_lose_kg': 0,
                'avg_burned': round(avg_burned_per_day),
                'message': 'You are already at or below your target body fat percentage!'
            })
            
        current_fat = weight * (estimated_bf / 100.0)
        lean_mass = weight * (1.0 - (estimated_bf / 100.0))
        target_weight = lean_mass / (1.0 - (target_bf / 100.0))
        fat_to_lose = weight - target_weight
        
        # 1 kg fat = ~7700 kcal
        total_calories_to_lose = fat_to_lose * 7700.0
        # Calculate timeline based on average calories burned daily
        days_needed = total_calories_to_lose / avg_burned_per_day
        
        return jsonify({
            'success': True,
            'estimated_bf': round(estimated_bf, 1),
            'current_fat_kg': round(current_fat, 2),
            'lean_mass_kg': round(lean_mass, 2),
            'target_weight_kg': round(target_weight, 2),
            'fat_to_lose_kg': round(fat_to_lose, 2),
            'total_calories_needed': round(total_calories_to_lose),
            'avg_burned': round(avg_burned_per_day),
            'days_needed': round(days_needed, 1)
        })
        
    except Exception as e:
        return jsonify({'error': f"Internal server error during analysis: {str(e)}"}), 500

# ================= OPERATOR RANK SYSTEM =================

OPERATOR_RANKS = [
    {
        "id": "recruit",
        "title": "Recruit",
        "subtitle": "Day 1. Everyone starts here.",
        "min_score": 0,
        "color": "#555555",
        "description": "You have entered the program. Prove you belong.",
        "perks": ["Access to calorie tracking", "Basic AI food estimation"]
    },
    {
        "id": "trainee",
        "title": "Trainee",
        "subtitle": "You showed up. Keep showing up.",
        "min_score": 15,
        "color": "#7a5c2e",
        "description": "Consistency is starting to form. The hardest part is behind you.",
        "perks": ["Trainee status badge", "Streak tracking unlocked"]
    },
    {
        "id": "operative",
        "title": "Operative",
        "subtitle": "Discipline is taking shape.",
        "min_score": 35,
        "color": "#b35520",
        "description": "You log, you burn, you control. An operative is dangerous when consistent.",
        "perks": ["Operative badge", "AI debrief access", "Weekly performance summary"]
    },
    {
        "id": "specialist",
        "title": "Specialist",
        "subtitle": "Most people quit here. You didn't.",
        "min_score": 60,
        "color": "#e8702a",
        "description": "Your habits compound. Specialists know the game and play it ruthlessly.",
        "perks": ["Specialist badge", "Priority AI analysis", "Detailed macro insights"]
    },
    {
        "id": "operator",
        "title": "Operator",
        "subtitle": "Elite. Few reach this.",
        "min_score": 85,
        "color": "#f0c060",
        "description": "Operators do not rely on motivation. They run on systems, discipline, and data.",
        "perks": ["Operator badge", "Full AI coaching debrief", "Body transformation timeline"]
    }
]

def compute_operator_score(logs, user):
    """
    Score 0-100 based on:
    - Logging streak (days with at least one food log): up to 30 pts
    - Workout consistency (days with burned log): up to 25 pts
    - Calorie discipline (days within 200 kcal of 2000 target): up to 25 pts
    - Total volume (sheer amount of data logged): up to 20 pts
    """
    if not logs:
        return 0, {}

    # Group by date
    dates_consumed = {}
    dates_burned = {}
    for log in logs:
        d = log.get('date', '')
        if log['type'] == 'consumed':
            dates_consumed[d] = dates_consumed.get(d, 0) + log['calories']
        elif log['type'] == 'burned':
            dates_burned[d] = dates_burned.get(d, 0) + log['calories']

    all_dates = sorted(set(list(dates_consumed.keys()) + list(dates_burned.keys())))
    total_days = max(len(all_dates), 1)

    # 1. Logging streak score (days logged food / total days, weighted)
    days_with_food = len(dates_consumed)
    streak_score = min(30, round((days_with_food / total_days) * 30))

    # 2. Workout consistency
    days_with_workout = len(dates_burned)
    workout_score = min(25, round((days_with_workout / total_days) * 25))

    # 3. Calorie discipline (consumed within ±300 of a 2000 kcal target)
    disciplined_days = sum(1 for d in dates_consumed if 1700 <= dates_consumed[d] <= 2300)
    discipline_score = min(25, round((disciplined_days / max(len(dates_consumed), 1)) * 25))

    # 4. Volume score (just having a decent amount of data)
    volume_score = min(20, len(logs) // 3)

    total = streak_score + workout_score + discipline_score + volume_score

    stats = {
        "total_score": total,
        "days_logged": days_with_food,
        "days_trained": days_with_workout,
        "disciplined_days": disciplined_days,
        "total_days": total_days,
        "streak_score": streak_score,
        "workout_score": workout_score,
        "discipline_score": discipline_score,
        "volume_score": volume_score,
        "avg_consumed": round(sum(dates_consumed.values()) / max(len(dates_consumed), 1)),
        "avg_burned": round(sum(dates_burned.values()) / max(len(dates_burned), 1)) if dates_burned else 0,
    }
    return total, stats


def get_rank_for_score(score):
    current = OPERATOR_RANKS[0]
    for rank in OPERATOR_RANKS:
        if score >= rank["min_score"]:
            current = rank
    return current


def get_next_rank(current_rank_id):
    ids = [r["id"] for r in OPERATOR_RANKS]
    idx = ids.index(current_rank_id)
    if idx < len(OPERATOR_RANKS) - 1:
        return OPERATOR_RANKS[idx + 1]
    return None


@app.route('/api/operator-status')
def operator_status():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    users = query_supabase('users', 'GET', params={'id': f'eq.{user_id}'})
    if not users:
        return jsonify({'error': 'User not found'}), 404
    user = users[0]

    logs = query_supabase('logs', 'GET', params={'user_id': f'eq.{user_id}'}) or []
    score, stats = compute_operator_score(logs, user)
    rank = get_rank_for_score(score)
    next_rank = get_next_rank(rank["id"])

    pts_to_next = (next_rank["min_score"] - score) if next_rank else 0

    return jsonify({
        'success': True,
        'score': score,
        'rank': rank,
        'next_rank': next_rank,
        'pts_to_next': pts_to_next,
        'stats': stats,
        'all_ranks': OPERATOR_RANKS
    })


@app.route('/api/operator-debrief', methods=['POST'])
def operator_debrief():
    """Generate a personalized AI debrief using Groq LLM based on the user's performance stats."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401

    users = query_supabase('users', 'GET', params={'id': f'eq.{user_id}'})
    if not users:
        return jsonify({'error': 'User not found'}), 404
    user = users[0]

    logs = query_supabase('logs', 'GET', params={'user_id': f'eq.{user_id}'}) or []
    score, stats = compute_operator_score(logs, user)
    rank = get_rank_for_score(score)
    next_rank = get_next_rank(rank["id"])

    system_prompt = (
        "You are a world-class performance coach and sports scientist. Your communication style is direct, "
        "precise, and commanding — like a special forces trainer. You do not sugarcoat. You do not use filler phrases. "
        "You deliver concise, honest, motivating debriefs. Never use emojis. Never use bullet points. Write in short, punchy paragraphs. "
        "Keep total response under 120 words."
    )

    user_prompt = (
        f"Athlete: {user.get('name', 'Athlete')}, Age {user.get('age', '?')}, "
        f"Weight {user.get('weight', '?')} kg, Height {user.get('height', '?')} cm.\n"
        f"Current Rank: {rank['title']}.\n"
        f"Operator Score: {score}/100.\n"
        f"Days with food logged: {stats.get('days_logged', 0)} out of {stats.get('total_days', 0)}.\n"
        f"Days with workout logged: {stats.get('days_trained', 0)}.\n"
        f"Days within calorie discipline target: {stats.get('disciplined_days', 0)}.\n"
        f"Average daily intake: {stats.get('avg_consumed', 0)} kcal. Average burned: {stats.get('avg_burned', 0)} kcal.\n"
        f"Next rank to unlock: {next_rank['title'] if next_rank else 'Already at peak rank'}.\n\n"
        "Deliver a performance debrief. Identify their biggest weakness, acknowledge what's working, "
        "and give them one ruthless directive to improve their rank. Address them by name."
    )

    try:
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=200,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )
        debrief_text = chat.choices[0].message.content.strip()
        return jsonify({'success': True, 'debrief': debrief_text, 'rank': rank, 'score': score, 'stats': stats})
    except Exception as e:
        return jsonify({'error': f"Debrief generation failed: {str(e)}"}), 500



# ================= LEADERBOARD =================

@app.route('/api/leaderboard')
def leaderboard():
    """Return all users ranked by their operator score, publicly visible."""
    all_users = query_supabase('users', 'GET') or []
    current_user_id = session.get('user_id')

    entries = []
    for u in all_users:
        uid = u.get('id')
        logs = query_supabase('logs', 'GET', params={'user_id': f'eq.{uid}'}) or []
        score, stats = compute_operator_score(logs, u)
        rank = get_rank_for_score(score)
        entries.append({
            'user_id': uid,
            'name': u.get('name', 'Unknown'),
            'username': u.get('username', ''),
            'score': score,
            'rank': rank,
            'days_logged': stats.get('days_logged', 0),
            'days_trained': stats.get('days_trained', 0),
            'is_me': uid == current_user_id,
        })

    entries.sort(key=lambda x: x['score'], reverse=True)
    for i, e in enumerate(entries):
        e['position'] = i + 1

    return jsonify({'success': True, 'entries': entries})



# ── WATER TRACKING ──────────────────────────────────────────────────────────

@app.route('/api/water/today', methods=['GET'])
def get_water_today():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    today_str = date.today().isoformat()
    records = query_supabase('water', 'GET', params={'user_id': f'eq.{user_id}', 'date': f'eq.{today_str}'}) or []
    total_ml = sum(r.get('ml', 0) for r in records)
    return jsonify({'success': True, 'total_ml': total_ml, 'entries': len(records)})

@app.route('/api/water/add', methods=['POST'])
def add_water():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    ml = int(data.get('ml', 250))
    today_str = date.today().isoformat()
    entry = {
        'user_id': user_id,
        'ml': ml,
        'date': today_str,
        'timestamp': datetime.now().strftime('%H:%M:%S')
    }
    query_supabase('water', 'POST', data=entry)
    records = query_supabase('water', 'GET', params={'user_id': f'eq.{user_id}', 'date': f'eq.{today_str}'}) or []
    total_ml = sum(r.get('ml', 0) for r in records)
    return jsonify({'success': True, 'total_ml': total_ml})

@app.route('/api/water/reset', methods=['POST'])
def reset_water():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    today_str = date.today().isoformat()
    query_supabase('water', 'DELETE', params={'user_id': f'eq.{user_id}', 'date': f'eq.{today_str}'})
    return jsonify({'success': True, 'total_ml': 0})

@app.route('/api/water/history', methods=['GET'])
def get_water_history():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    records = query_supabase('water', 'GET', params={'user_id': f'eq.{user_id}'}) or []
    daily = {}
    for r in records:
        d = r.get('date', '')
        daily[d] = daily.get(d, 0) + r.get('ml', 0)
    return jsonify({'success': True, 'daily': daily})

@app.route('/api/macros/today', methods=['GET'])
def get_macros_today():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({'error': 'Unauthorized'}), 401
    today_str = date.today().isoformat()
    logs = query_supabase('logs', 'GET', params={'user_id': f'eq.{user_id}'}) or []
    today_logs = [l for l in logs if l.get('type') == 'consumed' and l.get('date') == today_str]
    totals = {'protein': 0, 'carbs': 0, 'fat': 0, 'calories': 0}
    for l in today_logs:
        totals['protein'] += int(l.get('protein') or 0)
        totals['carbs']   += int(l.get('carbs') or 0)
        totals['fat']     += int(l.get('fat') or 0)
        totals['calories'] += int(l.get('calories') or 0)
    return jsonify({'success': True, **totals})

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
