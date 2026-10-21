"""Texts the code sends itself (R1, R3, R11): disclosures, confirmations, handoffs, fallbacks.

Numbers, dates and payment details in these messages come from the database, never from the
model. Wording avoids grammatical gender of the bot ("Вас записано", not "я записав").
A human proofreads every change here.
"""

from datetime import date

from app.brain.i18n import lang_or_default

TEMPLATES: dict[str, dict[str, str]] = {
    "disclosure_first": {
        "uk": "Вітаю! Я AI-асистент студії «{studio}».",
        "ru": "Здравствуйте! Я AI-ассистент студии «{studio}».",
        "en": "Hello! I'm the AI assistant of {studio}.",
        "pl": "Dzień dobry! Jestem asystentem AI studia „{studio}”.",
    },
    "disclosure_return": {
        "uk": "На зв'язку знову AI-асистент студії «{studio}».",
        "ru": "На связи снова AI-ассистент студии «{studio}».",
        "en": "The AI assistant of {studio} is back with you.",
        "pl": "Ponownie pisze asystent AI studia „{studio}”.",
    },
    "booking_confirmed": {
        "uk": "{name}, вас записано: {service}, {day} о {time}.{master} Вартість: {price}. "
        "Адреса: {address}. Якщо плани зміняться, напишіть сюди.",
        "ru": "{name}, вы записаны: {service}, {day} в {time}.{master} Стоимость: {price}. "
        "Адрес: {address}. Если планы изменятся, напишите сюда.",
        "en": "{name}, you're booked: {service}, {day} at {time}.{master} Price: {price}. "
        "Address: {address}. If your plans change, just write here.",
        "pl": "{name}, wizyta potwierdzona: {service}, {day}, godz. {time}.{master} "
        "Cena: {price}. Adres: {address}. Jeśli plany się zmienią, proszę napisać tutaj.",
    },
    "booking_reserved": {
        "uk": "{name}, час заброньовано: {service}, {day} о {time}.{master} Вартість: {price}. "
        "Адреса: {address}.",
        "ru": "{name}, время забронировано: {service}, {day} в {time}.{master} "
        "Стоимость: {price}. Адрес: {address}.",
        "en": "{name}, the time is reserved: {service}, {day} at {time}.{master} "
        "Price: {price}. Address: {address}.",
        "pl": "{name}, termin zarezerwowany: {service}, {day}, godz. {time}.{master} "
        "Cena: {price}. Adres: {address}.",
    },
    "prepayment_request": {
        "uk": "Щоб підтвердити запис, потрібна передоплата {amount} до {deadline}. {requisites} "
        "Після оплати надішліть, будь ласка, скриншот сюди.",
        "ru": "Чтобы подтвердить запись, нужна предоплата {amount} до {deadline}. {requisites} "
        "После оплаты пришлите, пожалуйста, скриншот сюда.",
        "en": "To confirm the booking, please prepay {amount} by {deadline}. {requisites} "
        "After paying, please send a screenshot here.",
        "pl": "Aby potwierdzić wizytę, prosimy o przedpłatę {amount} do {deadline}. {requisites} "
        "Po opłaceniu prosimy przesłać tutaj zrzut ekranu.",
    },
    "prepayment_confirmed": {
        "uk": "Оплату отримано, запис підтверджено: {service}, {day} о {time}. До зустрічі!",
        "ru": "Оплата получена, запись подтверждена: {service}, {day} в {time}. До встречи!",
        "en": "Payment received, your booking is confirmed: {service}, {day} at {time}. See you!",
        "pl": "Płatność otrzymana, wizyta potwierdzona: {service}, {day}, godz. {time}. "
        "Do zobaczenia!",
    },
    "request_received": {
        "uk": "{name}, заявку прийнято: {service}, бажаний час: {wish}. "
        "Студія підтвердить точний час {eta}.",
        "ru": "{name}, заявка принята: {service}, желаемое время: {wish}. "
        "Студия подтвердит точное время {eta}.",
        "en": "{name}, your request is received: {service}, preferred time: {wish}. "
        "The studio will confirm the exact time {eta}.",
        "pl": "{name}, zgłoszenie przyjęte: {service}, preferowany termin: {wish}. "
        "Studio potwierdzi dokładną godzinę {eta}.",
    },
    "request_confirmed": {
        "uk": "Студія підтвердила ваш запис: {service}, {day} о {time}. Адреса: {address}. "
        "Якщо плани зміняться, напишіть сюди.",
        "ru": "Студия подтвердила вашу запись: {service}, {day} в {time}. Адрес: {address}. "
        "Если планы изменятся, напишите сюда.",
        "en": "The studio confirmed your booking: {service}, {day} at {time}. "
        "Address: {address}. If your plans change, just write here.",
        "pl": "Studio potwierdziło wizytę: {service}, {day}, godz. {time}. Adres: {address}. "
        "Jeśli plany się zmienią, proszę napisać tutaj.",
    },
    "request_rejected": {
        "uk": "На жаль, на цей час записати не вийде. Напишіть, будь ласка, інший зручний день "
        "і час.",
        "ru": "К сожалению, на это время записать не получится. Напишите, пожалуйста, другой "
        "удобный день и время.",
        "en": "Unfortunately this time doesn't work. Please suggest another day and time.",
        "pl": "Niestety ten termin nie jest dostępny. Proszę zaproponować inny dzień i godzinę.",
    },
    "booking_cancelled": {
        "uk": "Запис на {day} о {time} скасовано. Будемо раді бачити вас іншим разом.",
        "ru": "Запись на {day} в {time} отменена. Будем рады видеть вас в другой раз.",
        "en": "Your booking for {day} at {time} is cancelled. Hope to see you another time.",
        "pl": "Wizyta {day}, godz. {time} została odwołana. Zapraszamy innym razem.",
    },
    "request_cancelled": {
        "uk": "Заявку скасовано. Будемо раді бачити вас іншим разом.",
        "ru": "Заявка отменена. Будем рады видеть вас в другой раз.",
        "en": "Your request is cancelled. Hope to see you another time.",
        "pl": "Zgłoszenie zostało anulowane. Zapraszamy innym razem.",
    },
    "booking_rescheduled": {
        "uk": "Запис перенесено: {service}, {day} о {time}. Адреса: {address}.",
        "ru": "Запись перенесена: {service}, {day} в {time}. Адрес: {address}.",
        "en": "Your booking is moved: {service}, {day} at {time}. Address: {address}.",
        "pl": "Wizyta przeniesiona: {service}, {day}, godz. {time}. Adres: {address}.",
    },
    "handoff": {
        "uk": "Це питання краще уточнити в студії. Ваше повідомлення вже передано, "
        "вам відповідять тут {eta}.",
        "ru": "Этот вопрос лучше уточнить в студии. Ваше сообщение уже передано, "
        "вам ответят здесь {eta}.",
        "en": "This is best answered by the studio. Your message has been passed on, "
        "and they will reply here {eta}.",
        "pl": "To pytanie najlepiej omówić ze studiem. Wiadomość została przekazana, "
        "odpowiedź pojawi się tutaj {eta}.",
    },
    "handoff_media": {
        "uk": "Дякуємо за фото! Його вже передано в студію, вам відповідять тут {eta}.",
        "ru": "Спасибо за фото! Его уже передали в студию, вам ответят здесь {eta}.",
        "en": "Thanks for the photo! It has been passed to the studio, "
        "and they will reply here {eta}.",
        "pl": "Dziękujemy za zdjęcie! Zostało przekazane do studia, "
        "odpowiedź pojawi się tutaj {eta}.",
    },
    "handoff_voice": {
        "uk": "Дякуємо! Голосове вже передано в студію, вам відповідять тут {eta}.",
        "ru": "Спасибо! Голосовое уже передали в студию, вам ответят здесь {eta}.",
        "en": "Thanks! Your voice message has been passed to the studio, "
        "and they will reply here {eta}.",
        "pl": "Dziękujemy! Wiadomość głosowa została przekazana do studia, "
        "odpowiedź pojawi się tutaj {eta}.",
    },
    "holding": {
        "uk": "Ваше повідомлення бачать у студії, вам відповідять тут {eta}.",
        "ru": "Ваше сообщение видят в студии, вам ответят здесь {eta}.",
        "en": "The studio has your message and will reply here {eta}.",
        "pl": "Wiadomość trafiła do studia, odpowiedź pojawi się tutaj {eta}.",
    },
    "voice_request": {
        "uk": "Вибачте, голосові повідомлення поки не прослуховуються. Напишіть, будь ласка, "
        "текстом.",
        "ru": "Извините, голосовые сообщения пока не прослушиваются. Напишите, пожалуйста, "
        "текстом.",
        "en": "Sorry, voice messages can't be played here yet. Please type your message.",
        "pl": "Przepraszamy, wiadomości głosowe nie są jeszcze odsłuchiwane. Proszę napisać "
        "tekstem.",
    },
    "fallback": {
        "uk": "Дякуємо за повідомлення! Вам відповідять тут {eta}.",
        "ru": "Спасибо за сообщение! Вам ответят здесь {eta}.",
        "en": "Thanks for your message! The studio will reply here {eta}.",
        "pl": "Dziękujemy za wiadomość! Odpowiedź pojawi się tutaj {eta}.",
    },
    "privacy_notice": {
        "uk": "Ім'я та телефон потрібні лише для запису. Як ми зберігаємо дані: {url}",
        "ru": "Имя и телефон нужны только для записи. Как мы храним данные: {url}",
        "en": "Your name and phone are used only for the booking. How we store data: {url}",
        "pl": "Imię i telefon są potrzebne tylko do zapisu. Jak przechowujemy dane: {url}",
    },
    "demo_welcome": {
        "uk": "Вітаю! Я AI-асистент демо-студії «{studio}». Запитайте про ціни, вільний час "
        "або запишіться. Це демонстрація: дані вигадані, записи пробні.",
        "ru": "Здравствуйте! Я AI-ассистент демо-студии «{studio}». Спросите о ценах, "
        "свободном времени или запишитесь. Это демонстрация: данные вымышлены, записи пробные.",
        "en": "Hello! I'm the AI assistant of the demo studio {studio}. Ask about prices, "
        "free time or book a visit. This is a demo: the data is made up and bookings are "
        "for testing.",
        "pl": "Dzień dobry! Jestem asystentem AI studia demonstracyjnego „{studio}”. Proszę "
        "zapytać o ceny, wolne terminy lub zapisać się. To demo: dane są zmyślone, a zapisy "
        "testowe.",
    },
}

MASTER_LABEL = {"uk": "Майстер", "ru": "Мастер", "en": "Master", "pl": "Prowadzi"}

_ETA = {
    "uk": {
        "soon": "найближчим часом",
        "today": "сьогодні після {time}",
        "tomorrow": "завтра після {time}",
        "weekday": "{weekday} після {time}",
    },
    "ru": {
        "soon": "в ближайшее время",
        "today": "сегодня после {time}",
        "tomorrow": "завтра после {time}",
        "weekday": "{weekday} после {time}",
    },
    "en": {
        "soon": "shortly",
        "today": "today after {time}",
        "tomorrow": "tomorrow after {time}",
        "weekday": "{weekday} after {time}",
    },
    "pl": {
        "soon": "wkrótce",
        "today": "dzisiaj po {time}",
        "tomorrow": "jutro po {time}",
        "weekday": "{weekday} po {time}",
    },
}

# "у середу", "в среду", "we wtorek": weekday with its preposition, accusative case.
_ON_WEEKDAY = {
    "uk": (
        "у понеділок",
        "у вівторок",
        "у середу",
        "у четвер",
        "у п'ятницю",
        "у суботу",
        "у неділю",
    ),
    "ru": (
        "в понедельник",
        "во вторник",
        "в среду",
        "в четверг",
        "в пятницу",
        "в субботу",
        "в воскресенье",
    ),
    "en": (
        "on Monday",
        "on Tuesday",
        "on Wednesday",
        "on Thursday",
        "on Friday",
        "on Saturday",
        "on Sunday",
    ),
    "pl": (
        "w poniedziałek",
        "we wtorek",
        "w środę",
        "w czwartek",
        "w piątek",
        "w sobotę",
        "w niedzielę",
    ),
}


def render(key: str, lang: str, **values: object) -> str:
    return TEMPLATES[key][lang_or_default(lang)].format(**values)


def master_suffix(master: str | None, lang: str) -> str:
    if not master:
        return ""
    return f" {MASTER_LABEL[lang_or_default(lang)]}: {master}."


def on_weekday(day: date, lang: str) -> str:
    return _ON_WEEKDAY[lang_or_default(lang)][day.weekday()]


def eta_phrase(kind: str, lang: str, *, time: str = "", day: date | None = None) -> str:
    lang = lang_or_default(lang)
    weekday = on_weekday(day, lang) if day else ""
    return _ETA[lang][kind].format(time=time, weekday=weekday)
