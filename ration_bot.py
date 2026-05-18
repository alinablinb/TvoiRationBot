import random
from datetime import date, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
from io import BytesIO
import asyncio
from dotenv import load_dotenv
import os

# ==================== ЗАГРУЗКА СЕКРЕТОВ ====================
load_dotenv()

# ==================== НАСТРОЙКИ ====================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# НАСТРОЙКИ POSTGRESQL
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', 5432),
    'database': os.getenv('DB_NAME', 'nutrition_bot'),
    'user': os.getenv('DB_USER', 'postgres'),
    'password': os.getenv('DB_PASSWORD')
}

# Состояния опроса
AGE, HEIGHT, WEIGHT, GENDER, ACTIVITY, GOAL, ALLERGIES = range(7)

# Состояния для админ-входа
ADMIN_AWAITING_PASSWORD = 20

# Состояния для рассылки
BROADCAST_TEXT = 10

# Состояние для обновления веса
WEIGHT_UPDATE = 40

# Временные данные
user_data_temp = {}

# Уровни активности
ACTIVITY_LEVELS = {
    '1': 1.2,
    '2': 1.375,
    '3': 1.55,
    '4': 1.725
}

ACTIVITY_NAMES = {
    '1': 'Сидячий (офисная работа)',
    '2': 'Умеренный (тренировки 2-3 раза/нед)',
    '3': 'Высокий (тренировки 4-5 раз/нед)',
    '4': 'Очень высокий (физическая работа)'
}

GOAL_NAMES = {
    'lose': 'Похудение',
    'maintain': 'Поддержание веса',
    'gain': 'Набор массы'
}

MEAL_TYPE_NAMES = {
    'breakfast': 'Завтрак',
    'lunch': 'Обед',
    'dinner': 'Ужин',
    'snack': 'Перекус'
}

ALLERGENS_RU = {
    'nuts': 'Орехи',
    'dairy': 'Молочные продукты',
    'gluten': 'Глютен',
    'eggs': 'Яйца',
    'seafood': 'Морепродукты'
}


# ==================== РАБОТА С БД ====================

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=DB_CONFIG['host'],
            port=DB_CONFIG['port'],
            database=DB_CONFIG['database'],
            user=DB_CONFIG['user'],
            password=DB_CONFIG['password']
        )
        return conn
    except Exception as e:
        print(f"Ошибка подключения: {e}")
        return None


def save_user(user_id, user_data):
    conn = get_db_connection()
    if not conn:
        return False

    cur = conn.cursor()

    try:
        # Проверяем, существует ли пользователь
        cur.execute('SELECT initial_weight, created_at FROM users WHERE user_id = %s', (user_id,))
        existing = cur.fetchone()

        # Определяем initial_weight: если уже был, оставляем старый
        if existing and existing[0] is not None:
            initial_weight = existing[0]
        else:
            initial_weight = user_data.get('weight')

        # Текущий вес
        current_weight = user_data.get('weight')

        # Дата создания
        if existing and existing[1] is not None:
            created_at = existing[1]
        else:
            created_at = None

        cur.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, age, height, weight, 
                             gender, activity_factor, goal, allergies, calories, protein, fat, carbs, 
                             initial_weight, current_weight, created_at, last_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 
                    COALESCE(%s, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                age = EXCLUDED.age,
                height = EXCLUDED.height,
                weight = EXCLUDED.weight,
                gender = EXCLUDED.gender,
                activity_factor = EXCLUDED.activity_factor,
                goal = EXCLUDED.goal,
                allergies = EXCLUDED.allergies,
                calories = EXCLUDED.calories,
                protein = EXCLUDED.protein,
                fat = EXCLUDED.fat,
                carbs = EXCLUDED.carbs,
                current_weight = EXCLUDED.weight,
                last_active = CURRENT_TIMESTAMP,
                initial_weight = CASE 
                    WHEN users.initial_weight IS NULL THEN EXCLUDED.weight
                    ELSE users.initial_weight
                END
        """, (
            user_id,
            user_data.get('username'),
            user_data.get('first_name'),
            user_data.get('last_name'),
            user_data['age'],
            user_data['height'],
            user_data['weight'],
            user_data['gender'],
            user_data['activity_factor'],
            user_data['goal'],
            user_data.get('allergies', []),
            user_data['calories'],
            user_data['protein'],
            user_data['fat'],
            user_data['carbs'],
            initial_weight,
            current_weight,
            created_at
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"Ошибка сохранения пользователя: {e}")
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def get_user_params(user_id):
    conn = get_db_connection()
    if not conn:
        return None

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute('SELECT goal, allergies, calories, protein, fat, carbs FROM users WHERE user_id = %s', (user_id,))
        return cur.fetchone()
    except Exception as e:
        print(f"Ошибка получения параметров: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def get_menu_without_allergens(goal, meal_type, allergies):
    conn = get_db_connection()
    if not conn:
        return []

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        if allergies:
            cur.execute("""
                SELECT mt.id, mt.goal, mt.meal_type, mt.name,
                       (SELECT calories FROM calculate_dish_nutrition(mt.id)) as calories,
                       (SELECT protein FROM calculate_dish_nutrition(mt.id)) as protein,
                       (SELECT fat FROM calculate_dish_nutrition(mt.id)) as fat,
                       (SELECT carbs FROM calculate_dish_nutrition(mt.id)) as carbs,
                       COALESCE(
                            (SELECT STRING_AGG(p.name || ' - ' || di.quantity_grams || ' г', '\n')
                            FROM dish_ingredients di
                            JOIN products p ON di.product_id = p.id
                            WHERE di.menu_id = mt.id),
                           'Ингредиенты не указаны'
                       ) as ingredients,
                       mt.instructions,
                       mt.likes,
                       mt.dislikes,
                       mt.avg_rating
                FROM menu_templates mt
                WHERE mt.goal = %s AND mt.meal_type = %s AND mt.is_active = TRUE
                AND NOT EXISTS (
                    SELECT 1 FROM menu_allergens ma
                    WHERE ma.menu_id = mt.id AND ma.allergen_name = ANY(%s)
                )
            """, (goal, meal_type, allergies))
        else:
            cur.execute("""
                SELECT mt.id, mt.goal, mt.meal_type, mt.name,
                       (SELECT calories FROM calculate_dish_nutrition(mt.id)) as calories,
                       (SELECT protein FROM calculate_dish_nutrition(mt.id)) as protein,
                       (SELECT fat FROM calculate_dish_nutrition(mt.id)) as fat,
                       (SELECT carbs FROM calculate_dish_nutrition(mt.id)) as carbs,
                       COALESCE(
                           (SELECT STRING_AGG(p.name || ' - ' || di.quantity_grams || ' г', '\n')
                            FROM dish_ingredients di
                            JOIN products p ON di.product_id = p.id
                            WHERE di.menu_id = mt.id),
                           'Ингредиенты не указаны'
                       ) as ingredients,
                       mt.instructions,
                       mt.likes,
                       mt.dislikes,
                       mt.avg_rating
                FROM menu_templates mt
                WHERE mt.goal = %s AND mt.meal_type = %s AND mt.is_active = TRUE
            """, (goal, meal_type))

        return cur.fetchall()
    except Exception as e:
        print(f"Ошибка получения меню: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def get_all_users():
    conn = get_db_connection()
    if not conn:
        return []

    cur = conn.cursor()

    try:
        cur.execute("SELECT user_id FROM users")
        return cur.fetchall()
    except Exception as e:
        print(f"Ошибка получения пользователей: {e}")
        return []
    finally:
        cur.close()
        conn.close()


def check_user_exists(user_id):
    conn = get_db_connection()
    if not conn:
        return False

    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        return cur.fetchone() is not None
    except Exception as e:
        print(f"Ошибка проверки пользователя: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def add_like(user_id, menu_id):
    """Добавляет лайк блюду"""
    conn = get_db_connection()
    if not conn:
        return False, None

    if not check_user_exists(user_id):
        print(f"Пользователь {user_id} не найден в БД")
        return False, None

    cur = conn.cursor()

    try:
        cur.execute("SELECT rating FROM user_ratings WHERE user_id = %s AND menu_id = %s", (user_id, menu_id))
        existing = cur.fetchone()

        if existing:
            old_rating = existing[0]
            if old_rating == 1:
                # Убираем лайк
                cur.execute("UPDATE menu_templates SET likes = likes - 1 WHERE id = %s", (menu_id,))
                cur.execute("DELETE FROM user_ratings WHERE user_id = %s AND menu_id = %s", (user_id, menu_id))
                result = "unliked"
            elif old_rating == -1:
                # Меняем дизлайк на лайк
                cur.execute("UPDATE menu_templates SET dislikes = dislikes - 1, likes = likes + 1 WHERE id = %s",
                            (menu_id,))
                cur.execute(
                    "UPDATE user_ratings SET rating = 1, created_at = CURRENT_TIMESTAMP WHERE user_id = %s AND menu_id = %s",
                    (user_id, menu_id))
                result = "changed_to_like"
            else:
                cur.execute("UPDATE menu_templates SET likes = likes + 1 WHERE id = %s", (menu_id,))
                cur.execute(
                    "INSERT INTO user_ratings (user_id, menu_id, rating, created_at) VALUES (%s, %s, 1, CURRENT_TIMESTAMP)",
                    (user_id, menu_id))
                result = "liked"
        else:
            cur.execute("UPDATE menu_templates SET likes = likes + 1 WHERE id = %s", (menu_id,))
            cur.execute(
                "INSERT INTO user_ratings (user_id, menu_id, rating, created_at) VALUES (%s, %s, 1, CURRENT_TIMESTAMP)",
                (user_id, menu_id))
            result = "liked"

        conn.commit()
        return True, result
    except Exception as e:
        print(f"Ошибка при добавлении лайка: {e}")
        conn.rollback()
        return False, None
    finally:
        cur.close()
        conn.close()


def add_dislike(user_id, menu_id):
    """Добавляет дизлайк блюду"""
    conn = get_db_connection()
    if not conn:
        return False, None

    if not check_user_exists(user_id):
        print(f"Пользователь {user_id} не найден в БД")
        return False, None

    cur = conn.cursor()

    try:
        cur.execute("SELECT rating FROM user_ratings WHERE user_id = %s AND menu_id = %s", (user_id, menu_id))
        existing = cur.fetchone()

        if existing:
            old_rating = existing[0]
            if old_rating == -1:
                # Убираем дизлайк
                cur.execute("UPDATE menu_templates SET dislikes = dislikes - 1 WHERE id = %s", (menu_id,))
                cur.execute("DELETE FROM user_ratings WHERE user_id = %s AND menu_id = %s", (user_id, menu_id))
                result = "undisliked"
            elif old_rating == 1:
                # Меняем лайк на дизлайк
                cur.execute("UPDATE menu_templates SET likes = likes - 1, dislikes = dislikes + 1 WHERE id = %s",
                            (menu_id,))
                cur.execute(
                    "UPDATE user_ratings SET rating = -1, created_at = CURRENT_TIMESTAMP WHERE user_id = %s AND menu_id = %s",
                    (user_id, menu_id))
                result = "changed_to_dislike"
            else:
                cur.execute("UPDATE menu_templates SET dislikes = dislikes + 1 WHERE id = %s", (menu_id,))
                cur.execute(
                    "INSERT INTO user_ratings (user_id, menu_id, rating, created_at) VALUES (%s, %s, -1, CURRENT_TIMESTAMP)",
                    (user_id, menu_id))
                result = "disliked"
        else:
            cur.execute("UPDATE menu_templates SET dislikes = dislikes + 1 WHERE id = %s", (menu_id,))
            cur.execute(
                "INSERT INTO user_ratings (user_id, menu_id, rating, created_at) VALUES (%s, %s, -1, CURRENT_TIMESTAMP)",
                (user_id, menu_id))
            result = "disliked"

        conn.commit()
        return True, result
    except Exception as e:
        print(f"Ошибка при добавлении дизлайка: {e}")
        conn.rollback()
        return False, None
    finally:
        cur.close()
        conn.close()


def get_user_rating(user_id, menu_id):
    """Возвращает оценку пользователя для блюда: 1 (лайк), -1 (дизлайк) или 0"""
    conn = get_db_connection()
    if not conn:
        return 0

    cur = conn.cursor()
    try:
        cur.execute("SELECT rating FROM user_ratings WHERE user_id = %s AND menu_id = %s", (user_id, menu_id))
        result = cur.fetchone()
        return result[0] if result else 0
    except Exception as e:
        print(f"Ошибка получения рейтинга: {e}")
        return 0
    finally:
        cur.close()
        conn.close()


def get_rating_info(menu_id):
    """Возвращает лайки, дизлайки и средний рейтинг (0-5)"""
    conn = get_db_connection()
    if not conn:
        return 0, 0, 0.0

    cur = conn.cursor()
    try:
        cur.execute("SELECT likes, dislikes, avg_rating FROM menu_templates WHERE id = %s", (menu_id,))
        result = cur.fetchone()
        if result:
            likes = result[0] or 0
            dislikes = result[1] or 0
            avg_rating = result[2] or 0.0
            return likes, dislikes, avg_rating
        return 0, 0, 0.0
    except Exception as e:
        print(f"Ошибка получения информации о рейтинге: {e}")
        return 0, 0, 0.0
    finally:
        cur.close()
        conn.close()


# ==================== ФУНКЦИИ ДЛЯ ОТСЛЕЖИВАНИЯ ПРОГРЕССА ====================

def update_user_weight(user_id, new_weight):
    conn = get_db_connection()
    if not conn:
        return False, "Ошибка подключения к БД"

    cur = conn.cursor()

    try:
        cur.execute('SELECT initial_weight, current_weight FROM users WHERE user_id = %s', (user_id,))
        user = cur.fetchone()

        if not user:
            return False, "Пользователь не найден"

        initial_weight = user[0]

        if initial_weight is None:
            initial_weight = new_weight

        cur.execute('''
            UPDATE users 
            SET current_weight = %s, initial_weight = COALESCE(initial_weight, %s)
            WHERE user_id = %s
        ''', (new_weight, initial_weight, user_id))

        conn.commit()

        return True, {
            'initial_weight': initial_weight,
            'current_weight': new_weight,
            'total_change': round(new_weight - initial_weight, 1)
        }
    except Exception as e:
        print(f"Ошибка обновления веса: {e}")
        return False, str(e)
    finally:
        cur.close()
        conn.close()


def get_user_progress(user_id):
    conn = get_db_connection()
    if not conn:
        return None

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute('''
            SELECT initial_weight, current_weight, goal, calories
            FROM users 
            WHERE user_id = %s
        ''', (user_id,))
        return cur.fetchone()
    except Exception as e:
        print(f"Ошибка получения прогресса: {e}")
        return None
    finally:
        cur.close()
        conn.close()


def format_progress_message(progress_data):
    if not progress_data:
        return "Данные о прогрессе не найдены. Пройдите опрос с помощью /start."

    initial = progress_data.get('initial_weight')
    current = progress_data.get('current_weight')
    goal = progress_data.get('goal')

    if initial is None:
        return "У вас пока нет данных о весе. Используйте /weight чтобы добавить свой текущий вес."

    if current is None:
        current = initial

    total_change = current - initial
    change_text = f"-{abs(total_change)}" if total_change < 0 else f"+{total_change}" if total_change > 0 else "0"

    message = f"Ваш прогресс\n\n"
    message += f"Цель: {GOAL_NAMES.get(goal, goal)}\n"
    message += f"Начальный вес: {initial} кг\n"
    message += f"Текущий вес: {current} кг\n"
    message += f"Изменение: {change_text} кг"

    return message


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def calculate_calories(weight, height, age, gender, activity_factor, goal):
    if gender == 'male':
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161

    maintenance = bmr * activity_factor

    if goal == 'lose':
        return max(maintenance - 500, 1200)
    elif goal == 'gain':
        return maintenance + 300
    else:
        return maintenance


def generate_weekly_menu(goal, allergies):
    weekly_menu = {}
    meal_types = ['breakfast', 'lunch', 'dinner', 'snack']

    for day in range(1, 8):
        day_menu = {}
        for meal_type in meal_types:
            dishes = get_menu_without_allergens(goal, meal_type, allergies)
            if dishes:
                dish = random.choice(dishes)
                day_menu[meal_type] = {
                    'id': dish['id'],
                    'name': dish['name'],
                    'calories': dish['calories'],
                    'protein': dish['protein'],
                    'fat': dish['fat'],
                    'carbs': dish['carbs'],
                    'ingredients': dish['ingredients'],
                    'instructions': dish['instructions']
                }
        weekly_menu[day] = day_menu

    return weekly_menu


def format_recipe(dish, user_rating=0, likes=0, dislikes=0, avg_rating=0):
    """Форматирует рецепт с отображением звёздного рейтинга"""
    if not dish:
        return "Рецепт не найден"

    # Создаём звёздочки для визуализации
    if avg_rating > 0:
        full_stars = int(avg_rating)
        empty_stars = 5 - full_stars
        stars = "⭐" * full_stars + "☆" * empty_stars
        rating_text = f"{avg_rating}/5 {stars}"
    else:
        rating_text = "Нет оценок"

    text = f"*{dish['name']}*\n\n"
    text += f"*КБЖУ:* {dish['calories']} ккал | Белки: {dish['protein']}г | Жиры: {dish['fat']}г | Углеводы: {dish['carbs']}г\n\n"
    text += f"*Ингредиенты:*\n{dish['ingredients']}\n\n"
    text += f"*Приготовление:*\n{dish['instructions']}\n\n"
    text += f"⭐ *Рейтинг:* {rating_text}\n"
    text += f"👍 {likes} | 👎 {dislikes}"

    return text


# ==================== ЭКСПОРТ В EXCEL ====================

def export_to_excel_full():
    conn = get_db_connection()
    if not conn:
        return None

    output = BytesIO()

    try:
        users_df = pd.read_sql("""
            SELECT user_id, username, first_name, gender, goal, 
                   created_at, last_active
            FROM users 
            ORDER BY created_at DESC
        """, conn)
        if not users_df.empty:
            users_df.columns = ['ID пользователя', 'Username', 'Имя', 'Пол', 'Цель',
                                'Дата регистрации', 'Последняя активность']

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM menu_templates")
        total_dishes = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= NOW() - INTERVAL '7 days'")
        active_week = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= NOW() - INTERVAL '30 days'")
        active_month = cur.fetchone()[0]
        cur.close()

        stats_df = pd.DataFrame({
            'Показатель': ['Всего пользователей', 'Всего блюд в меню',
                           'Активных за неделю', 'Активных за месяц'],
            'Значение': [total_users, total_dishes, active_week, active_month]
        })

        dishes_df = pd.read_sql("""
            SELECT name, meal_type, goal,
                   likes,
                   dislikes,
                   avg_rating,
                   CASE 
                       WHEN likes + dislikes > 0 THEN ROUND((likes * 100.0) / (likes + dislikes), 1)
                       ELSE 0
                   END as like_percentage
            FROM menu_templates 
            ORDER BY avg_rating DESC, likes DESC
        """, conn)

        if not dishes_df.empty:
            meal_type_translate = {'breakfast': 'Завтрак', 'lunch': 'Обед', 'dinner': 'Ужин', 'snack': 'Перекус'}
            goal_translate = {'lose': 'Похудение', 'maintain': 'Поддержание', 'gain': 'Набор массы'}
            dishes_df['meal_type'] = dishes_df['meal_type'].map(meal_type_translate)
            dishes_df['goal'] = dishes_df['goal'].map(goal_translate)
            dishes_df.columns = ['Название блюда', 'Тип приёма', 'Цель', 'Лайков', 'Дизлайков',
                                 'Рейтинг (0-5)', 'Процент одобрения']

        goals_df = pd.read_sql("""
            SELECT 
                CASE goal
                    WHEN 'lose' THEN 'Похудение'
                    WHEN 'maintain' THEN 'Поддержание веса'
                    WHEN 'gain' THEN 'Набор массы'
                    ELSE 'Не указана'
                END as goal_name,
                COUNT(*) as count
            FROM users
            GROUP BY goal
            ORDER BY count DESC
        """, conn)
        if not goals_df.empty:
            goals_df.columns = ['Цель', 'Количество пользователей']

        allergies_df = pd.read_sql("""
            SELECT unnest(allergies) as allergy, COUNT(*) as count
            FROM users
            WHERE allergies IS NOT NULL AND array_length(allergies, 1) > 0
            GROUP BY unnest(allergies)
            ORDER BY count DESC
        """, conn)

        if not allergies_df.empty:
            allergy_translate = {'nuts': 'Орехи', 'dairy': 'Молочные', 'gluten': 'Глютен',
                                 'eggs': 'Яйца', 'seafood': 'Морепродукты'}
            allergies_df['allergy'] = allergies_df['allergy'].map(allergy_translate)
            allergies_df.columns = ['Аллерген', 'Количество пользователей']

        conn.close()

        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            stats_df.to_excel(writer, sheet_name='Общая статистика', index=False)
            if not users_df.empty:
                users_df.to_excel(writer, sheet_name='Пользователи', index=False)
            if not dishes_df.empty:
                dishes_df.to_excel(writer, sheet_name='Рейтинг блюд', index=False)
            if not goals_df.empty:
                goals_df.to_excel(writer, sheet_name='Цели пользователей', index=False)
            if not allergies_df.empty:
                allergies_df.to_excel(writer, sheet_name='Аллергии', index=False)

            for sheet_name in writer.sheets:
                worksheet = writer.sheets[sheet_name]
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    adjusted_width = min(max_length + 2, 50)
                    worksheet.column_dimensions[column_letter].width = adjusted_width

        output.seek(0)
        return output

    except Exception as e:
        print(f"Ошибка экспорта в Excel: {e}")
        return None


# ==================== КОМАНДЫ ====================

async def set_commands(application):
    commands = [
        BotCommand("start", "Начать опрос"),
        BotCommand("newplan", "Новое меню"),
        BotCommand("mynorm", "Моя норма"),
        BotCommand("progress", "Мой прогресс"),
        BotCommand("weight", "Обновить вес"),
        BotCommand("help", "Помощь"),
        BotCommand("admin", "Админ панель"),
    ]
    await application.bot.set_my_commands(commands)
    print("Кнопки в нижней панели установлены")


async def unknown_command(update, context):
    if context.user_data.get('in_conversation'):
        return

    await update.message.reply_text(
        "Пожалуйста, используйте одну из доступных команд:\n"
        "/start - начать опрос\n"
        "/newplan - новое меню\n"
        "/mynorm - моя норма калорий\n"
        "/progress - мой прогресс\n"
        "/weight - обновить вес\n"
        "/help - помощь\n"
        "/admin - админ-панель\n\n"
        "Или нажмите на кнопку в меню снизу."
    )


async def start(update, context):
    user_id = update.effective_user.id
    user = update.effective_user

    # Очищаем временные данные
    if user_id in user_data_temp:
        del user_data_temp[user_id]

    context.user_data.clear()

    conn = get_db_connection()
    has_data = False
    if conn:
        cur = conn.cursor()
        cur.execute('SELECT user_id FROM users WHERE user_id = %s', (user_id,))
        has_data = cur.fetchone() is not None
        cur.close()
        conn.close()

    if has_data:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Да, обновить данные", callback_data="confirm_update_data")],
            [InlineKeyboardButton("Нет, вернуться в меню", callback_data="cancel_update_data")]
        ])
        await update.message.reply_text(
            "Вы уже проходили опрос ранее.\n\n"
            "Хотите обновить свои данные или вернуться в главное меню?",
            reply_markup=keyboard
        )
        return ConversationHandler.END

    # Новый пользователь
    user_data_temp[user_id] = {
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'age': None,
        'height': None,
        'weight': None,
        'gender': None,
        'activity_factor': None,
        'goal': None,
        'allergies': []
    }

    await update.message.reply_text(
        "Рекомендации по использованию бота:\n\n"
        "1. Пройдите опрос, чтобы получить персональный план питания на неделю\n"
        "2. Следуйте плану питания в течение недели\n"
        "3. Через неделю обновите свой вес с помощью команды /weight\n"
        "4. Бот пересчитает вашу норму калорий и предложит обновить меню\n"
        "5. Вы можете продолжить питаться по старому меню или сгенерировать новое\n\n"
        "Регулярное обновление веса помогает отслеживать прогресс и корректировать рацион.\n\n"
        "Давайте начнём! Сколько вам лет? (введите число)"
    )
    return AGE


async def confirm_update_data_callback(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user = update.effective_user

    # Полностью очищаем все данные
    if user_id in user_data_temp:
        del user_data_temp[user_id]

    # Создаём новые временные данные
    user_data_temp[user_id] = {
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'age': None,
        'height': None,
        'weight': None,
        'gender': None,
        'activity_factor': None,
        'goal': None,
        'allergies': []
    }

    await query.message.delete()
    await query.message.reply_text(
        "Начинаем опрос заново!\n\n"
        "Сколько вам лет? (введите число)"
    )

    # Возвращаем AGE для продолжения диалога
    return AGE


async def cancel_update_callback(update, context):
    query = update.callback_query
    await query.answer()

    await query.message.edit_text(
        "Возвращаюсь в главное меню.\n\n"
        "Используйте команды из меню для навигации:\n"
        "/newplan - новое меню\n"
        "/mynorm - моя норма калорий\n"
        "/progress - мой прогресс\n"
        "/weight - обновить вес"
    )

    return ConversationHandler.END


async def newplan_command(update, context):
    user_id = update.effective_user.id

    if not check_user_exists(user_id):
        await update.message.reply_text(
            "У вас нет сохранённых параметров.\n\n"
            "Пожалуйста, пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий и составить меню."
        )
        return

    user = get_user_params(user_id)

    if user and user.get('goal'):
        goal = user['goal']
        allergies = user['allergies'] or []

        await update.message.reply_text("Генерирую новое меню...")

        weekly_menu = generate_weekly_menu(goal, allergies)

        calories = user.get('calories', 2000)
        protein = user.get('protein', 150)
        fat = user.get('fat', 50)
        carbs = user.get('carbs', 200)

        context.user_data['weekly_menu'] = weekly_menu
        context.user_data['user_calories'] = calories
        context.user_data['user_bju'] = f"{protein}/{fat}/{carbs}"

        await send_weekly_menu(update.message, context)
    else:
        await update.message.reply_text(
            "У вас нет сохранённых параметров.\n\n"
            "Пожалуйста, пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий и составить меню."
        )


async def mynorm_command(update, context):
    user_id = update.effective_user.id

    if not check_user_exists(user_id):
        await update.message.reply_text(
            "У вас нет сохранённых параметров.\n\n"
            "Пожалуйста, пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий."
        )
        return

    user = get_user_params(user_id)

    if user and user.get('calories'):
        text = (
            "Ваша дневная норма:\n\n"
            f"Калории: {int(user['calories'])} ккал\n"
            f"Белки: {int(user['protein'])} г\n"
            f"Жиры: {int(user['fat'])} г\n"
            f"Углеводы: {int(user['carbs'])} г\n\n"
            f"Цель: {GOAL_NAMES.get(user['goal'], 'Не выбрана')}"
        )
        await update.message.reply_text(text)
    else:
        await update.message.reply_text(
            "У вас нет сохранённых параметров.\n\n"
            "Пожалуйста, пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий."
        )


async def progress_command(update, context):
    user_id = update.effective_user.id

    if not check_user_exists(user_id):
        await update.message.reply_text(
            "У вас нет данных о прогрессе.\n\n"
            "Пожалуйста, пройдите опрос с помощью команды /start."
        )
        return

    progress_data = get_user_progress(user_id)
    message = format_progress_message(progress_data)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Обновить вес", callback_data="update_weight")]
    ])

    await update.message.reply_text(message, reply_markup=keyboard)


async def weight_command(update, context):
    user_id = update.effective_user.id

    # Проверяем, зарегистрирован ли пользователь
    if not check_user_exists(user_id):
        await update.message.reply_text(
            "Вы не зарегистрированы в системе.\n\n"
            "Пожалуйста, сначала пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий и сохранить ваши данные."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Обновление веса\n\n"
        "Введите ваш текущий вес в килограммах.\n"
        "Например: 65.5 или 70\n\n"
        "Для отмены введите /cancel"
    )
    return WEIGHT_UPDATE


async def process_weight_update(update, context):
    user_id = update.effective_user.id

    # Проверяем, зарегистрирован ли пользователь
    if not check_user_exists(user_id):
        await update.message.reply_text(
            "Вы не зарегистрированы в системе.\n\n"
            "Пожалуйста, сначала пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий и сохранить ваши данные."
        )
        return ConversationHandler.END

    try:
        new_weight = float(update.message.text.replace(',', '.'))

        if new_weight < 20 or new_weight > 300:
            await update.message.reply_text("Введите корректный вес (20-300 кг).")
            return WEIGHT_UPDATE

        old_user = get_user_params(user_id)
        old_calories = old_user.get('calories') if old_user else None

        success, result = update_user_weight(user_id, new_weight)

        if success:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute('SELECT age, height, gender, activity_factor, goal FROM users WHERE user_id = %s',
                            (user_id,))
                user_data = cur.fetchone()
                cur.close()
                conn.close()

                if user_data:
                    age, height, gender, activity_factor, goal = user_data
                    new_calories = calculate_calories(new_weight, height, age, gender, activity_factor, goal)
                    new_protein = round(new_calories * 0.3 / 4)
                    new_fat = round(new_calories * 0.25 / 9)
                    new_carbs = round(new_calories * 0.45 / 4)

                    conn2 = get_db_connection()
                    cur2 = conn2.cursor()
                    cur2.execute('''
                        UPDATE users 
                        SET calories = %s, protein = %s, fat = %s, carbs = %s
                        WHERE user_id = %s
                    ''', (new_calories, new_protein, new_fat, new_carbs, user_id))
                    conn2.commit()
                    cur2.close()
                    conn2.close()

                    await update.message.reply_text(
                        f"Вес обновлён!\n\n"
                        f"Начальный вес: {result['initial_weight']} кг\n"
                        f"Текущий вес: {result['current_weight']} кг\n"
                        f"Изменение: {result['total_change']:+} кг\n\n"
                        f"Норма калорий пересчитана:\n"
                        f"   Было: {int(old_calories)} ккал\n"
                        f"   Стало: {int(new_calories)} ккал\n\n"
                        f"Хотите обновить меню?",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Да, обновить меню", callback_data="update_menu_after_weight")],
                            [InlineKeyboardButton("Нет, оставить текущее", callback_data="skip_menu_update")]
                        ])
                    )
                    return ConversationHandler.END

        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("Введите число (используйте точку или запятую).")
        return WEIGHT_UPDATE


async def update_weight_callback(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    # Проверяем, зарегистрирован ли пользователь
    if not check_user_exists(user_id):
        await query.message.reply_text(
            "Вы не зарегистрированы в системе.\n\n"
            "Пожалуйста, сначала пройдите опрос с помощью команды /start,\n"
            "чтобы я мог рассчитать вашу норму калорий и сохранить ваши данные."
        )
        return ConversationHandler.END

    await query.message.reply_text(
        "Обновление веса\n\n"
        "Введите ваш текущий вес в килограммах.\n"
        "Например: 65.5 или 70\n\n"
        "Для отмены введите /cancel"
    )
    return WEIGHT_UPDATE


async def update_menu_after_weight_callback(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user = get_user_params(user_id)

    if user and user.get('goal'):
        goal = user['goal']
        allergies = user['allergies'] or []

        await query.message.reply_text("Генерирую новое меню...")

        weekly_menu = generate_weekly_menu(goal, allergies)

        calories = user.get('calories', 2000)
        protein = user.get('protein', 150)
        fat = user.get('fat', 50)
        carbs = user.get('carbs', 200)

        context.user_data['weekly_menu'] = weekly_menu
        context.user_data['user_calories'] = calories
        context.user_data['user_bju'] = f"{protein}/{fat}/{carbs}"

        await send_weekly_menu_from_callback(query, context)
    else:
        await query.message.reply_text("Ошибка при генерации меню")


async def skip_menu_update_callback(update, context):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Меню не изменено. Вы можете сгенерировать новое позже командой /newplan")


async def help_command(update, context):
    await update.message.reply_text(
        "*Инструкция по использованию бота*\n\n"
        "/start - Пройти опрос и получить меню\n"
        "/newplan - Создать новое меню\n"
        "/mynorm - Показать норму калорий\n"
        "/progress - Показать прогресс\n"
        "/weight - Обновить текущий вес\n"
        "/help - Помощь\n"
        "/admin - Админ-панель (только для администратора)\n\n"
        "⭐ Оценивайте рецепты: 👍 или 👎 под каждым блюдом",
        parse_mode='Markdown'
    )


# ==================== ОБРАБОТЧИКИ ОПРОСА ====================

async def process_age(update, context):
    user_id = update.effective_user.id

    if user_id not in user_data_temp:
        await update.message.reply_text(
            "У вас нет активного опроса.\n"
            "Используйте /start чтобы начать."
        )
        return ConversationHandler.END

    try:
        age = int(update.message.text)
        if age < 10 or age > 120:
            await update.message.reply_text("Введите реальный возраст (10-120 лет).")
            return AGE
        user_data_temp[user_id]['age'] = age
        await update.message.reply_text("Какой у вас рост? (в см)")
        return HEIGHT
    except ValueError:
        await update.message.reply_text("Введите число.")
        return AGE


async def process_height(update, context):
    user_id = update.effective_user.id

    if user_id not in user_data_temp:
        await update.message.reply_text(
            "У вас нет активного опроса.\n"
            "Используйте /start чтобы начать."
        )
        return ConversationHandler.END

    try:
        height = int(update.message.text)
        if height < 80 or height > 250:
            await update.message.reply_text("Введите корректный рост (80-250 см).")
            return HEIGHT
        user_data_temp[user_id]['height'] = height
        await update.message.reply_text("Сколько вы весите? (в кг)")
        return WEIGHT
    except ValueError:
        await update.message.reply_text("Введите число.")
        return HEIGHT


async def process_weight(update, context):
    user_id = update.effective_user.id

    if user_id not in user_data_temp:
        await update.message.reply_text(
            "У вас нет активного опроса.\n"
            "Используйте /start чтобы начать."
        )
        return ConversationHandler.END

    try:
        weight = float(update.message.text.replace(',', '.'))
        if weight < 20 or weight > 300:
            await update.message.reply_text("Введите корректный вес (20-300 кг).")
            return WEIGHT
        user_data_temp[user_id]['weight'] = weight

        keyboard = [
            [InlineKeyboardButton("Мужской", callback_data='gender_male')],
            [InlineKeyboardButton("Женский", callback_data='gender_female')]
        ]
        await update.message.reply_text("Выберите свой пол:", reply_markup=InlineKeyboardMarkup(keyboard))
        return GENDER
    except ValueError:
        await update.message.reply_text("Введите число.")
        return WEIGHT


async def gender_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in user_data_temp:
        await query.answer("Нет активного опроса", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    gender = 'male' if query.data == 'gender_male' else 'female'
    user_data_temp[user_id]['gender'] = gender

    keyboard = [
        [InlineKeyboardButton(ACTIVITY_NAMES['1'], callback_data='act_1')],
        [InlineKeyboardButton(ACTIVITY_NAMES['2'], callback_data='act_2')],
        [InlineKeyboardButton(ACTIVITY_NAMES['3'], callback_data='act_3')],
        [InlineKeyboardButton(ACTIVITY_NAMES['4'], callback_data='act_4')]
    ]
    await query.edit_message_text("Уровень физической активности:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ACTIVITY


async def activity_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in user_data_temp:
        await query.answer("Нет активного опроса", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    act_key = query.data.split('_')[1]
    user_data_temp[user_id]['activity_factor'] = ACTIVITY_LEVELS[act_key]

    keyboard = [
        [InlineKeyboardButton("Похудение", callback_data='goal_lose')],
        [InlineKeyboardButton("Поддержание веса", callback_data='goal_maintain')],
        [InlineKeyboardButton("Набор массы", callback_data='goal_gain')]
    ]
    await query.edit_message_text("Какая у вас цель?", reply_markup=InlineKeyboardMarkup(keyboard))
    return GOAL


async def goal_callback(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in user_data_temp:
        return ConversationHandler.END

    goal = query.data.split('_')[1]
    user_data_temp[user_id]['goal'] = goal

    keyboard = [
        [InlineKeyboardButton(ALLERGENS_RU['nuts'], callback_data='allergy_nuts')],
        [InlineKeyboardButton(ALLERGENS_RU['dairy'], callback_data='allergy_dairy')],
        [InlineKeyboardButton(ALLERGENS_RU['gluten'], callback_data='allergy_gluten')],
        [InlineKeyboardButton(ALLERGENS_RU['eggs'], callback_data='allergy_eggs')],
        [InlineKeyboardButton(ALLERGENS_RU['seafood'], callback_data='allergy_seafood')],
        [InlineKeyboardButton("Далее", callback_data='allergies_next')]
    ]

    await query.edit_message_text(
        "Есть ли у вас пищевые аллергии?\n\n"
        "Выберите аллергены из списка (можно несколько)\n"
        "Если аллергий нет, нажмите 'Далее'",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ALLERGIES


async def allergies_callback(update, context):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in user_data_temp:
        await query.answer("Ошибка", show_alert=True)
        return ConversationHandler.END

    if query.data == 'allergies_next':
        await generate_and_send_menu(update, context, user_id)
        return ConversationHandler.END

    allergy = query.data.replace('allergy_', '')

    if 'allergies' not in user_data_temp[user_id]:
        user_data_temp[user_id]['allergies'] = []

    if allergy in user_data_temp[user_id]['allergies']:
        user_data_temp[user_id]['allergies'].remove(allergy)
    else:
        user_data_temp[user_id]['allergies'].append(allergy)

    keyboard = []
    for key, name in ALLERGENS_RU.items():
        if key in user_data_temp[user_id]['allergies']:
            button_text = f"✅ {name}"
        else:
            button_text = f"{name}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f'allergy_{key}')])

    keyboard.append([InlineKeyboardButton("Далее", callback_data='allergies_next')])

    allergies_list = user_data_temp[user_id].get('allergies', [])
    allergies_text = ", ".join([ALLERGENS_RU.get(a, a) for a in allergies_list]) if allergies_list else "нет"

    await query.edit_message_text(
        f"Ваши аллергии: {allergies_text}\n\n"
        f"Выберите аллергены из списка (✅ - выбрано)\n"
        f"Если закончили, нажмите 'Далее'",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ALLERGIES


async def generate_and_send_menu(update, context, user_id):
    query = update.callback_query

    data = user_data_temp.get(user_id, {})
    age = data.get('age')
    height = data.get('height')
    weight = data.get('weight')
    gender = data.get('gender')
    activity = data.get('activity_factor')
    goal = data.get('goal')
    allergies = data.get('allergies', [])

    if not all([age, height, weight, gender, activity, goal]):
        await query.edit_message_text("Ошибка. Попробуйте /start заново")
        return

    calories = calculate_calories(weight, height, age, gender, activity, goal)
    protein = round(calories * 0.3 / 4)
    fat = round(calories * 0.25 / 9)
    carbs = round(calories * 0.45 / 4)

    weekly_menu = generate_weekly_menu(goal, allergies)

    user_full_data = {
        **data,
        'calories': round(calories),
        'protein': protein,
        'fat': fat,
        'carbs': carbs
    }

    if save_user(user_id, user_full_data):
        await query.edit_message_text("Отлично! Ваши данные сохранены. Генерирую меню на неделю...")
    else:
        await query.edit_message_text("Произошла ошибка при сохранении данных. Попробуйте позже.")
        return

    context.user_data['weekly_menu'] = weekly_menu
    context.user_data['user_calories'] = round(calories)
    context.user_data['user_bju'] = f"{protein}/{fat}/{carbs}"
    context.user_data['user_goal'] = goal

    await send_weekly_menu_from_callback(query, context)

    # Очищаем временные данные после успешного сохранения
    if user_id in user_data_temp:
        del user_data_temp[user_id]


async def send_weekly_menu(message_obj, context):
    weekly_menu = context.user_data.get('weekly_menu')
    if not weekly_menu:
        return

    calories = context.user_data.get('user_calories', 0)
    bju = context.user_data.get('user_bju', '0/0/0')

    await message_obj.reply_text(
        f"*Ваш план питания на неделю*\n\n"
        f"Норма: {calories} ккал/день\n"
        f"БЖУ: {bju} г\n\n"
        f"Нажмите на любое блюдо, чтобы получить рецепт и оценить его!\n\n"
        f"Дальнейшие действия можно выбрать в меню",
        parse_mode='Markdown'
    )

    meal_names = {
        'breakfast': 'Завтрак',
        'lunch': 'Обед',
        'dinner': 'Ужин',
        'snack': 'Перекус'
    }

    for day in range(1, 8):
        day_menu = weekly_menu[day]
        keyboard = []
        for meal_type, display_name in meal_names.items():
            dish = day_menu.get(meal_type)
            if dish:
                keyboard.append(
                    [InlineKeyboardButton(f"{display_name}: {dish['name']}", callback_data=f"recipe_{dish['id']}")])

        if keyboard:
            await message_obj.reply_text(f"📅 *День {day}:*", reply_markup=InlineKeyboardMarkup(keyboard),
                                         parse_mode='Markdown')


async def send_weekly_menu_from_callback(query, context):
    weekly_menu = context.user_data.get('weekly_menu')
    if not weekly_menu:
        return

    calories = context.user_data.get('user_calories', 0)
    bju = context.user_data.get('user_bju', '0/0/0')

    await query.message.reply_text(
        f"*Ваш план питания на неделю*\n\n"
        f"Норма: {calories} ккал/день\n"
        f"БЖУ: {bju} г\n\n"
        f"👇 Нажмите на любое блюдо, чтобы получить рецепт и оценить его!\n\n"
        f"Дальнейшие действия можно выбрать в меню",
        parse_mode='Markdown'
    )

    meal_names = {
        'breakfast': 'Завтрак',
        'lunch': 'Обед',
        'dinner': 'Ужин',
        'snack': 'Перекус'
    }

    for day in range(1, 8):
        day_menu = weekly_menu[day]
        keyboard = []
        for meal_type, display_name in meal_names.items():
            dish = day_menu.get(meal_type)
            if dish:
                keyboard.append(
                    [InlineKeyboardButton(f"{display_name}: {dish['name']}", callback_data=f"recipe_{dish['id']}")])

        if keyboard:
            await query.message.reply_text(f"📅 *День {day}:*", reply_markup=InlineKeyboardMarkup(keyboard),
                                           parse_mode='Markdown')


async def recipe_callback(update, context):
    query = update.callback_query
    await query.answer()

    dish_id = int(query.data.replace('recipe_', ''))
    user_id = query.from_user.id

    if not check_user_exists(user_id):
        await query.message.reply_text(
            "Пожалуйста, сначала пройдите регистрацию с помощью команды /start"
        )
        return

    conn = get_db_connection()
    if conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute('SELECT * FROM menu_templates WHERE id = %s', (dish_id,))
        dish = cur.fetchone()

        if dish:
            cur.execute('SELECT * FROM calculate_dish_nutrition(%s)', (dish_id,))
            nutrition = cur.fetchone()

            cur.execute("""
                SELECT STRING_AGG(p.name || ' - ' || di.quantity_grams || ' г', '\n') as ingredients
                FROM dish_ingredients di
                JOIN products p ON di.product_id = p.id
                WHERE di.menu_id = %s
            """, (dish_id,))
            ingredients = cur.fetchone()

            user_rating = get_user_rating(user_id, dish_id)
            likes, dislikes, avg_rating = get_rating_info(dish_id)

            dish_dict = {
                'id': dish['id'],
                'name': dish['name'],
                'calories': nutrition['calories'] if nutrition else 0,
                'protein': nutrition['protein'] if nutrition else 0,
                'fat': nutrition['fat'] if nutrition else 0,
                'carbs': nutrition['carbs'] if nutrition else 0,
                'ingredients': ingredients['ingredients'] if ingredients and ingredients[
                    'ingredients'] else 'Ингредиенты не указаны',
                'instructions': dish['instructions'],
            }

            recipe_text = format_recipe(dish_dict, user_rating, likes, dislikes, avg_rating)

            keyboard = []
            if user_rating == 1:
                keyboard.append(InlineKeyboardButton("✅ Лайк (вы уже оценили)", callback_data=f"like_{dish_id}"))
            else:
                keyboard.append(InlineKeyboardButton("👍 Лайк", callback_data=f"like_{dish_id}"))

            if user_rating == -1:
                keyboard.append(InlineKeyboardButton("❌ Дизлайк (вы уже оценили)", callback_data=f"dislike_{dish_id}"))
            else:
                keyboard.append(InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike_{dish_id}"))

            await query.message.reply_text(
                recipe_text,
                reply_markup=InlineKeyboardMarkup([keyboard]),
                parse_mode='Markdown'
            )
        else:
            await query.message.reply_text("Рецепт не найден")

        cur.close()
        conn.close()


async def like_callback(update, context):
    query = update.callback_query
    await query.answer()

    dish_id = int(query.data.replace('like_', ''))
    user_id = query.from_user.id

    success, result = add_like(user_id, dish_id)

    if success:
        if result == "liked":
            await query.message.reply_text("👍 Спасибо за оценку! Вы отметили рецепт как хороший.")
        elif result == "unliked":
            await query.message.reply_text("👍 Вы убрали свою оценку.")
        elif result == "changed_to_like":
            await query.message.reply_text("👍 Вы изменили оценку на хорошую!")

        await recipe_callback(update, context)
    else:
        await query.message.reply_text("Ошибка при сохранении оценки")


async def dislike_callback(update, context):
    query = update.callback_query
    await query.answer()

    dish_id = int(query.data.replace('dislike_', ''))
    user_id = query.from_user.id

    success, result = add_dislike(user_id, dish_id)

    if success:
        if result == "disliked":
            await query.message.reply_text("👎 Спасибо за оценку! Мы учтём это.")
        elif result == "undisliked":
            await query.message.reply_text("👎 Вы убрали свою оценку.")
        elif result == "changed_to_dislike":
            await query.message.reply_text("👎 Вы изменили оценку на плохую!")

        await recipe_callback(update, context)
    else:
        await query.message.reply_text("Ошибка при сохранении оценки")


async def cancel(update, context):
    user_id = update.effective_user.id

    if user_id in user_data_temp:
        del user_data_temp[user_id]

    await update.message.reply_text(
        "Опрос отменён.\n\n"
        "Чтобы начать заново, используйте команду /start"
    )
    return ConversationHandler.END


# ==================== АДМИН-ФУНКЦИИ ====================

def is_admin(context):
    return context.user_data.get('is_admin', False)


async def admin_command(update, context):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Отменить вход", callback_data="cancel_admin_login")]
    ])

    await update.message.reply_text(
        "Вход в админ-панель\n\n"
        "Введите пароль администратора.\n\n"
        "Для отмены нажмите кнопку ниже:",
        reply_markup=keyboard
    )
    return ADMIN_AWAITING_PASSWORD


async def cancel_admin_login_callback(update, context):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "Вход в админ-панель отменён.\n\n"
        "Используйте /admin когда будете готовы."
    )
    return ConversationHandler.END


async def process_admin_password(update, context):
    password = update.message.text

    if password == ADMIN_PASSWORD:
        context.user_data['is_admin'] = True

        keyboard = [
            [InlineKeyboardButton("Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("Экспорт в Excel", callback_data="admin_export")],
            [InlineKeyboardButton("Массовая рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton("Выход", callback_data="admin_exit")]
        ]

        await update.message.reply_text(
            "✅ Доступ разрешён!\n\n"
            "Добро пожаловать в админ-панель.\n\n"
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Отменить вход", callback_data="cancel_admin_login")]
        ])

        await update.message.reply_text(
            "Неверный пароль!\n\n"
            "Попробуйте ещё раз или нажмите кнопку для отмены:",
            reply_markup=keyboard
        )
        return ADMIN_AWAITING_PASSWORD


async def admin_stats_callback(update, context):
    if not is_admin(context):
        await update.callback_query.answer("Нет доступа", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    conn = get_db_connection()
    if conn:
        cur = conn.cursor()

        cur.execute('SELECT COUNT(*) FROM users')
        total_users = cur.fetchone()[0]

        cur.execute('SELECT COUNT(*) FROM menu_templates')
        total_dishes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= NOW() - INTERVAL '7 days'")
        active_week = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= NOW() - INTERVAL '30 days'")
        active_month = cur.fetchone()[0]

        cur.execute("""
            SELECT name, 
                   likes,
                   dislikes,
                   avg_rating
            FROM menu_templates 
            WHERE likes + dislikes > 0 
            ORDER BY avg_rating DESC, likes DESC
            LIMIT 5
        """)
        top_dishes = cur.fetchall()

        cur.execute("""
            SELECT name, 
                   likes,
                   dislikes,
                   avg_rating
            FROM menu_templates 
            WHERE likes + dislikes > 0 
            ORDER BY avg_rating ASC, dislikes DESC
            LIMIT 5
        """)
        worst_dishes = cur.fetchall()

        cur.close()
        conn.close()

        text = f"*Статистика бота*\n\n"
        text += f"Пользователей: {total_users}\n"
        text += f"Активных за неделю: {active_week}\n"
        text += f"Активных за месяц: {active_month}\n"
        text += f"Всего блюд: {total_dishes}\n\n"

        text += "*ТОП-5 ЛУЧШИХ БЛЮД:*\n"
        if top_dishes:
            for i, dish in enumerate(top_dishes, 1):
                text += f"{i}. {dish[0]} - рейтинг: {dish[3]:.1f} (👍{dish[1]} 👎{dish[2]})\n"
        else:
            text += "   Нет блюд с оценками\n"

        text += "\n*ТОП-5 ХУДШИХ БЛЮД:*\n"
        if worst_dishes:
            for i, dish in enumerate(worst_dishes, 1):
                text += f"{i}. {dish[0]} - рейтинг: {dish[3]:.1f} (👍{dish[1]} 👎{dish[2]})\n"
        else:
            text += "   Нет блюд с оценками\n"

        await query.message.reply_text(text, parse_mode='Markdown')


async def admin_export_callback(update, context):
    if not is_admin(context):
        await update.callback_query.answer("Нет доступа", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    await query.message.reply_text("Формирую Excel-файл...")

    excel_file = await asyncio.to_thread(export_to_excel_full)

    if excel_file:
        await query.message.reply_document(
            document=excel_file,
            filename=f"bot_statistics_{date.today()}.xlsx",
            caption="Полная статистика бота"
        )
    else:
        await query.message.reply_text("Ошибка при формировании Excel-файла")


async def admin_broadcast_callback(update, context):
    if not is_admin(context):
        await update.callback_query.answer("Нет доступа", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Отменить рассылку", callback_data="cancel_broadcast")]
    ])

    await query.message.reply_text(
        "Массовая рассылка\n\nВведите сообщение для всех пользователей.\n\n"
        "Для отмены нажмите кнопку ниже:",
        reply_markup=keyboard
    )
    return BROADCAST_TEXT


async def cancel_broadcast_callback(update, context):
    query = update.callback_query
    await query.answer()

    await query.message.reply_text("Рассылка отменена.")
    return ConversationHandler.END


async def process_broadcast(update, context):
    if not is_admin(context):
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END

    message_text = update.message.text

    await update.message.reply_text("Начинаю рассылку...")

    users = get_all_users()

    if not users:
        await update.message.reply_text("Нет пользователей для рассылки")
        return ConversationHandler.END

    sent_count = 0
    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user[0],
                text=f"*Уведомление от администратора*\n\n{message_text}",
                parse_mode='Markdown'
            )
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            print(f"Ошибка отправки пользователю {user[0]}: {e}")

    await update.message.reply_text(f"Рассылка завершена!\nОтправлено: {sent_count}")
    return ConversationHandler.END


async def admin_exit_callback(update, context):
    if not is_admin(context):
        await update.callback_query.answer("Нет доступа", show_alert=True)
        return

    query = update.callback_query
    await query.answer()

    context.user_data['is_admin'] = False
    await query.message.reply_text("Вы вышли из админ-панели.")


# ==================== MAIN ====================

def main():
    print("Запуск бота...")

    conn = get_db_connection()
    if conn:
        print("Подключение к PostgreSQL успешно")
        conn.close()
    else:
        print("Ошибка подключения к БД")
        return

    application = Application.builder().token(TOKEN).build()

    application.post_init = set_commands

    # ConversationHandler для опроса
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CallbackQueryHandler(confirm_update_data_callback, pattern='^confirm_update_data$')
        ],
        states={
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_age)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_height)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_weight)],
            GENDER: [CallbackQueryHandler(gender_callback, pattern='^gender_')],
            ACTIVITY: [CallbackQueryHandler(activity_callback, pattern='^act_')],
            GOAL: [CallbackQueryHandler(goal_callback, pattern='^goal_')],
            ALLERGIES: [CallbackQueryHandler(allergies_callback, pattern='^(allergy_|allergies_next)')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)

    # ConversationHandler для админ-входа
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler('admin', admin_command)],
        states={
            ADMIN_AWAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_password)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CallbackQueryHandler(cancel_admin_login_callback, pattern='^cancel_admin_login$')
        ],
        allow_reentry=True
    )
    application.add_handler(admin_conv)

    # ConversationHandler для рассылки
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_callback, pattern='^admin_broadcast$')],
        states={
            BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_broadcast)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CallbackQueryHandler(cancel_broadcast_callback, pattern='^cancel_broadcast$')
        ],
        allow_reentry=True
    )
    application.add_handler(broadcast_conv)

    # ConversationHandler для обновления веса
    weight_conv = ConversationHandler(
        entry_points=[
            CommandHandler('weight', weight_command),
            CallbackQueryHandler(update_weight_callback, pattern='^update_weight$')
        ],
        states={
            WEIGHT_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_weight_update)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )
    application.add_handler(weight_conv)

    # Команды
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('newplan', newplan_command))
    application.add_handler(CommandHandler('mynorm', mynorm_command))
    application.add_handler(CommandHandler('progress', progress_command))
    application.add_handler(CommandHandler('weight', weight_command))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('cancel', cancel))

    # Обработчик для неизвестных команд
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_command))

    # Callback
    application.add_handler(CallbackQueryHandler(confirm_update_data_callback, pattern='^confirm_update_data$'))
    application.add_handler(CallbackQueryHandler(cancel_update_callback, pattern='^cancel_update_data$'))
    application.add_handler(CallbackQueryHandler(recipe_callback, pattern='^recipe_'))
    application.add_handler(CallbackQueryHandler(like_callback, pattern='^like_'))
    application.add_handler(CallbackQueryHandler(dislike_callback, pattern='^dislike_'))
    application.add_handler(CallbackQueryHandler(update_weight_callback, pattern='^update_weight$'))
    application.add_handler(
        CallbackQueryHandler(update_menu_after_weight_callback, pattern='^update_menu_after_weight$'))
    application.add_handler(CallbackQueryHandler(skip_menu_update_callback, pattern='^skip_menu_update$'))

    # Админ callback
    application.add_handler(CallbackQueryHandler(admin_stats_callback, pattern='^admin_stats$'))
    application.add_handler(CallbackQueryHandler(admin_export_callback, pattern='^admin_export$'))
    application.add_handler(CallbackQueryHandler(admin_broadcast_callback, pattern='^admin_broadcast$'))
    application.add_handler(CallbackQueryHandler(admin_exit_callback, pattern='^admin_exit$'))
    application.add_handler(CallbackQueryHandler(cancel_broadcast_callback, pattern='^cancel_broadcast$'))

    print("Бот успешно запущен и готов к работе!")
    application.run_polling()


if __name__ == '__main__':
    main()