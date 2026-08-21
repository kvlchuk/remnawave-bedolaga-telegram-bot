"""Покупка подарочной подписки прямо в боте.

Флоу: выбор тарифа → выбор периода → (опционально) поздравление → оплата.

После оплаты подарок остаётся в статусе PAID и превращается в передаваемую
claim-ссылку вида {CABINET_URL}/buy/gift/{token} — подписку получает тот, кто
перейдёт по ссылке и активирует её. Покупатель пересылает ссылку сам
(автоматически Telegram-получателям бот не пишет — защита от спуфинга
@username, см. notify_gift_claim_available).

Backend полностью переиспользуется:
  - app.services.guest_purchase_service.create_purchase
  - app.services.payment_service.PaymentService.create_guest_payment
"""

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.tariff import get_tariff_by_id, get_tariffs_for_user
from app.database.models import User
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

GIFT_ICON = '6032644646587338669'
TARIFF_ICON = '5258134813302332906'
CLOCK_ICON = '5258258882022612173'
BACK_ICON = '5258236805890710909'
HOME_ICON = '6042137469204303531'
PAY_ICON = '5258204546391351475'

MAX_GIFT_MESSAGE_LEN = 200
_USERNAME_RE = __import__('re').compile(r'^[a-zA-Z0-9_]{5,32}$')


class GiftPurchaseStates(StatesGroup):
    waiting_for_message = State()
    waiting_for_recipient = State()


def _back_row(texts, back_callback: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=texts.t('MENU_HOME_BUTTON', 'Меню'),
            icon_custom_emoji_id=HOME_ICON,
            callback_data='back_to_menu',
        ),
        InlineKeyboardButton(
            text=texts.BACK,
            icon_custom_emoji_id=BACK_ICON,
            callback_data=back_callback,
        ),
    ]


async def handle_gift_start(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Шаг 1 — список тарифов, доступных для подарка."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    tariffs = await get_tariffs_for_user(db, db_user.promo_group_id)

    keyboard: list[list[InlineKeyboardButton]] = []
    for tariff in tariffs:
        if not tariff.get_purchasable_periods():
            continue
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=tariff.name,
                    icon_custom_emoji_id=TARIFF_ICON,
                    callback_data=f'gift_tariff:{tariff.id}',
                )
            ]
        )

    if not keyboard:
        await callback.answer(
            texts.t('GIFT_NO_TARIFFS', 'Сейчас нет тарифов, доступных для подарка.'),
            show_alert=True,
        )
        return

    keyboard.append(_back_row(texts, 'menu_start'))

    await callback.message.edit_text(
        texts.t(
            'GIFT_SELECT_TARIFF',
            '<b>Подарить подписку</b>\n\nВыберите тариф, который хотите подарить:',
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def handle_gift_tariff(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Шаг 2 — выбор периода с ценами."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    try:
        tariff_id = int(callback.data.split(':', 1)[1])
    except (IndexError, ValueError):
        await callback.answer()
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active:
        await callback.answer(texts.t('GIFT_TARIFF_UNAVAILABLE', 'Тариф недоступен.'), show_alert=True)
        return

    keyboard: list[list[InlineKeyboardButton]] = []
    for period_days in tariff.get_purchasable_periods():
        price = tariff.get_purchasable_price_for_period(period_days)
        if price is None:
            continue
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f'{period_days} дн. — {settings.format_price(price)}',
                    icon_custom_emoji_id=CLOCK_ICON,
                    callback_data=f'gift_period:{tariff.id}:{period_days}',
                )
            ]
        )

    if not keyboard:
        await callback.answer(texts.t('GIFT_NO_PERIODS', 'Для этого тарифа нет доступных периодов.'), show_alert=True)
        return

    keyboard.append(_back_row(texts, 'gift_purchase_start'))

    await callback.message.edit_text(
        texts.t(
            'GIFT_SELECT_PERIOD',
            '<b>Подарить подписку</b>\n\nТариф: <b>{tariff}</b>\n\nВыберите срок:',
        ).replace('{tariff}', tariff.name),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


def _confirm_keyboard(
    texts, tariff_id: int, period_days: int, has_message: bool, recipient: str | None = None
) -> InlineKeyboardMarkup:
    message_label = (
        texts.t('GIFT_EDIT_MESSAGE', 'Изменить поздравление')
        if has_message
        else texts.t('GIFT_ADD_MESSAGE', 'Добавить поздравление')
    )
    recipient_label = (
        texts.t('GIFT_EDIT_RECIPIENT', 'Изменить получателя')
        if recipient
        else texts.t('GIFT_SET_RECIPIENT', 'Кому подарить')
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('GIFT_PAY_BUTTON', 'Оплатить'),
                    icon_custom_emoji_id=PAY_ICON,
                    style='primary',
                    callback_data=f'gift_pay:{tariff_id}:{period_days}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=recipient_label,
                    icon_custom_emoji_id=GIFT_ICON,
                    callback_data=f'gift_rcpt:{tariff_id}:{period_days}',
                )
            ],
            [
                InlineKeyboardButton(
                    text=message_label,
                    icon_custom_emoji_id=GIFT_ICON,
                    callback_data=f'gift_msg:{tariff_id}:{period_days}',
                )
            ],
            _back_row(texts, f'gift_tariff:{tariff_id}'),
        ]
    )


def _confirm_text(
    texts,
    tariff_name: str,
    period_days: int,
    price_kopeks: int,
    gift_message: str | None,
    recipient: str | None = None,
) -> str:
    text = texts.t(
        'GIFT_CONFIRM',
        '<b>Подарить подписку</b>\n\n'
        '<tg-emoji emoji-id="5258134813302332906">📦</tg-emoji> Тариф: <b>{tariff}</b>\n'
        '<tg-emoji emoji-id="5258258882022612173">⏰</tg-emoji> Срок: <b>{days} дн.</b>\n'
        '<tg-emoji emoji-id="5258204546391351475">💰</tg-emoji> Стоимость: <b>{price}</b>',
    )
    text = text.replace('{tariff}', tariff_name).replace('{days}', str(period_days))
    text = text.replace('{price}', settings.format_price(price_kopeks))

    if recipient:
        text += texts.t('GIFT_CONFIRM_RECIPIENT', '\n<tg-emoji emoji-id="6032644646587338669">🎁</tg-emoji> Получатель: <b>@{recipient}</b>').replace(
            '{recipient}', recipient
        )

    if gift_message:
        import html as html_mod

        text += texts.t('GIFT_CONFIRM_MESSAGE', '\n\nПоздравление:\n<blockquote>{message}</blockquote>').replace(
            '{message}', html_mod.escape(gift_message)
        )

    text += texts.t(
        'GIFT_CONFIRM_HINT',
        '\n\nПосле оплаты вы получите ссылку — перешлите её тому, кому дарите. '
        'Подписку активирует тот, кто перейдёт по ссылке.',
    )
    return text


async def _show_confirm(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    tariff_id: int,
    period_days: int,
) -> None:
    texts = get_texts(db_user.language)
    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active:
        await callback.answer(texts.t('GIFT_TARIFF_UNAVAILABLE', 'Тариф недоступен.'), show_alert=True)
        return

    price = tariff.get_purchasable_price_for_period(period_days)
    if price is None:
        await callback.answer(texts.t('GIFT_PRICE_UNAVAILABLE', 'Цена недоступна.'), show_alert=True)
        return

    data = await state.get_data()
    gift_message = data.get('gift_message')
    recipient = data.get('gift_recipient')

    await callback.message.edit_text(
        _confirm_text(texts, tariff.name, period_days, price, gift_message, recipient),
        reply_markup=_confirm_keyboard(texts, tariff_id, period_days, bool(gift_message), recipient),
    )
    await callback.answer()


async def handle_gift_period(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
) -> None:
    """Шаг 3 — экран подтверждения."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    try:
        _, tariff_id_raw, period_raw = callback.data.split(':', 2)
        tariff_id = int(tariff_id_raw)
        period_days = int(period_raw)
    except (IndexError, ValueError):
        await callback.answer()
        return

    await _show_confirm(callback, db_user, db, state, tariff_id, period_days)


async def handle_gift_recipient_prompt(
    callback: types.CallbackQuery, db_user: User, state: FSMContext
) -> None:
    """Экран выбора получателя: ссылка себе или @username друга."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    try:
        _, tariff_id_raw, period_raw = callback.data.split(':', 2)
        tariff_id = int(tariff_id_raw)
        period_days = int(period_raw)
    except (IndexError, ValueError):
        await callback.answer()
        return

    await state.update_data(gift_tariff_id=tariff_id, gift_period_days=period_days)
    await state.set_state(GiftPurchaseStates.waiting_for_recipient)

    await callback.message.edit_text(
        texts.t(
            'GIFT_RECIPIENT_PROMPT',
            '<b>Кому подарить?</b>\n\n'
            'Отправьте @username друга — бот сам вручит ему подарок, '
            'если он уже пользовался ботом.\n\n'
            'Либо получите ссылку и перешлите её сами.',
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_RECIPIENT_SELF', 'Просто получить ссылку'),
                        callback_data=f'gift_rcpt_clear:{tariff_id}:{period_days}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def handle_gift_recipient_clear(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
) -> None:
    """Сброс получателя — подарок выдаётся ссылкой покупателю."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    try:
        _, tariff_id_raw, period_raw = callback.data.split(':', 2)
        tariff_id = int(tariff_id_raw)
        period_days = int(period_raw)
    except (IndexError, ValueError):
        await callback.answer()
        return

    await state.update_data(gift_recipient=None)
    await state.set_state(None)
    await _show_confirm(callback, db_user, db, state, tariff_id, period_days)


async def handle_gift_recipient_input(
    message: types.Message, db_user: User, db: AsyncSession, state: FSMContext
) -> None:
    """Приём @username получателя с проверкой по базе."""
    from sqlalchemy import func, select

    texts = get_texts(db_user.language)
    raw = (message.text or '').strip().lstrip('@')

    if not _USERNAME_RE.match(raw):
        await message.answer(
            texts.t(
                'GIFT_RECIPIENT_INVALID',
                'Не похоже на username. Пришлите в формате @username (5-32 символа: буквы, цифры, _).',
            )
        )
        return

    data = await state.get_data()
    tariff_id = data.get('gift_tariff_id')
    period_days = data.get('gift_period_days')
    await state.update_data(gift_recipient=raw)
    await state.set_state(None)

    if not tariff_id or not period_days:
        await message.answer(texts.t('GIFT_SESSION_LOST', 'Сессия покупки истекла, начните заново.'))
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    price = tariff.get_purchasable_price_for_period(period_days) if tariff else None
    if tariff is None or price is None:
        await message.answer(texts.t('GIFT_TARIFF_UNAVAILABLE', 'Тариф недоступен.'))
        return

    result = await db.execute(select(User).where(func.lower(User.username) == raw.lower()))
    found = result.scalars().first()

    note = (
        texts.t('GIFT_RECIPIENT_FOUND', 'Пользователь найден — бот вручит подарок сам после оплаты.')
        if found and found.telegram_id
        else texts.t(
            'GIFT_RECIPIENT_NOT_FOUND',
            'Этот пользователь ещё не запускал бота — после оплаты перешлите ему ссылку сами.',
        )
    )

    gift_message = data.get('gift_message')
    await message.answer(
        _confirm_text(texts, tariff.name, period_days, price, gift_message, raw) + f'\n\n{note}',
        reply_markup=_confirm_keyboard(texts, tariff_id, period_days, bool(gift_message), raw),
    )


async def handle_gift_message_prompt(
    callback: types.CallbackQuery, db_user: User, state: FSMContext
) -> None:
    """Запрос текста поздравления."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    try:
        _, tariff_id_raw, period_raw = callback.data.split(':', 2)
        tariff_id = int(tariff_id_raw)
        period_days = int(period_raw)
    except (IndexError, ValueError):
        await callback.answer()
        return

    await state.update_data(gift_tariff_id=tariff_id, gift_period_days=period_days)
    await state.set_state(GiftPurchaseStates.waiting_for_message)

    await callback.message.edit_text(
        texts.t(
            'GIFT_MESSAGE_PROMPT',
            '<b>Поздравление к подарку</b>\n\nОтправьте текст (до {limit} символов) — '
            'его увидит получатель подарка.',
        ).replace('{limit}', str(MAX_GIFT_MESSAGE_LEN)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_SKIP_MESSAGE', 'Пропустить'),
                        callback_data=f'gift_period:{tariff_id}:{period_days}',
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def handle_gift_message_input(
    message: types.Message, db_user: User, db: AsyncSession, state: FSMContext
) -> None:
    """Приём текста поздравления."""
    texts = get_texts(db_user.language)
    text = (message.text or '').strip()

    if len(text) > MAX_GIFT_MESSAGE_LEN:
        await message.answer(
            texts.t('GIFT_MESSAGE_TOO_LONG', 'Слишком длинно — максимум {limit} символов.').replace(
                '{limit}', str(MAX_GIFT_MESSAGE_LEN)
            )
        )
        return

    data = await state.get_data()
    tariff_id = data.get('gift_tariff_id')
    period_days = data.get('gift_period_days')
    await state.update_data(gift_message=text)
    await state.set_state(None)

    if not tariff_id or not period_days:
        await message.answer(texts.t('GIFT_SESSION_LOST', 'Сессия покупки истекла, начните заново.'))
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    price = tariff.get_purchasable_price_for_period(period_days) if tariff else None
    if tariff is None or price is None:
        await message.answer(texts.t('GIFT_TARIFF_UNAVAILABLE', 'Тариф недоступен.'))
        return

    recipient = data.get('gift_recipient')
    await message.answer(
        _confirm_text(texts, tariff.name, period_days, price, text, recipient),
        reply_markup=_confirm_keyboard(texts, tariff_id, period_days, True, recipient),
    )


async def handle_gift_pay(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
) -> None:
    """Создание подарочной покупки и платежа."""
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    try:
        _, tariff_id_raw, period_raw = callback.data.split(':', 2)
        tariff_id = int(tariff_id_raw)
        period_days = int(period_raw)
    except (IndexError, ValueError):
        await callback.answer()
        return

    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None or not tariff.is_active:
        await callback.answer(texts.t('GIFT_TARIFF_UNAVAILABLE', 'Тариф недоступен.'), show_alert=True)
        return

    price_kopeks = tariff.get_purchasable_price_for_period(period_days)
    if price_kopeks is None:
        await callback.answer(texts.t('GIFT_PRICE_UNAVAILABLE', 'Цена недоступна.'), show_alert=True)
        return

    data = await state.get_data()
    gift_message = data.get('gift_message')
    recipient = data.get('gift_recipient')

    from app.services.guest_purchase_service import GuestPurchaseError, create_purchase

    contact_value = db_user.username or str(db_user.telegram_id)

    try:
        purchase = await create_purchase(
            db,
            landing=None,
            tariff=tariff,
            period_days=period_days,
            amount_kopeks=price_kopeks,
            contact_type='telegram',
            contact_value=contact_value,
            payment_method='telegram_stars',
            is_gift=True,
            gift_message=gift_message,
            gift_recipient_type='telegram' if recipient else None,
            gift_recipient_value=recipient or None,
            source='bot',
            buyer_user_id=db_user.id,
            commit=False,
        )
    except GuestPurchaseError as exc:
        logger.warning('Не удалось создать подарочную покупку', error=exc.message, user_id=db_user.id)
        await callback.answer(texts.t('GIFT_CREATE_FAILED', 'Не удалось оформить подарок.'), show_alert=True)
        return

    cabinet_base = (settings.CABINET_URL or '').rstrip('/')
    return_url = f'{cabinet_base}/gift/result?token={purchase.token[:12]}' if cabinet_base else ''

    from app.services.payment_service import PaymentService

    payment_service = PaymentService(bot=callback.bot)
    payment_result = await payment_service.create_guest_payment(
        db=db,
        amount_kopeks=price_kopeks,
        payment_method='telegram_stars',
        description=f'Подарок: {tariff.name} ({period_days} дн.)',
        purchase_token=purchase.token,
        return_url=return_url,
    )

    if not payment_result or not payment_result.get('payment_url'):
        await db.rollback()
        logger.error('Не удалось создать платёж для подарка', user_id=db_user.id, tariff_id=tariff.id)
        await callback.answer(
            texts.t('GIFT_PAYMENT_FAILED', 'Платёжная система недоступна, попробуйте позже.'),
            show_alert=True,
        )
        return

    await db.commit()
    await state.update_data(gift_message=None, gift_recipient=None)

    await callback.message.edit_text(
        texts.t(
            'GIFT_PAYMENT_READY',
            '<b>Подарок оформлен</b>\n\n'
            '<tg-emoji emoji-id="5258134813302332906">📦</tg-emoji> Тариф: <b>{tariff}</b>\n'
            '<tg-emoji emoji-id="5258258882022612173">⏰</tg-emoji> Срок: <b>{days} дн.</b>\n'
            '<tg-emoji emoji-id="5258204546391351475">💰</tg-emoji> К оплате: <b>{price}</b>\n\n'
            'Нажмите «Оплатить» — после оплаты придёт ссылка на подарок.',
        )
        .replace('{tariff}', tariff.name)
        .replace('{days}', str(period_days))
        .replace('{price}', settings.format_price(price_kopeks)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.t('GIFT_PAY_BUTTON', 'Оплатить'),
                        icon_custom_emoji_id=PAY_ICON,
                        style='primary',
                        url=payment_result['payment_url'],
                    )
                ],
                _back_row(texts, 'menu_start'),
            ]
        ),
    )
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_gift_start, F.data == 'gift_purchase_start')
    dp.callback_query.register(handle_gift_tariff, F.data.startswith('gift_tariff:'))
    dp.callback_query.register(handle_gift_period, F.data.startswith('gift_period:'))
    dp.callback_query.register(handle_gift_recipient_prompt, F.data.startswith('gift_rcpt:'))
    dp.callback_query.register(handle_gift_recipient_clear, F.data.startswith('gift_rcpt_clear:'))
    dp.callback_query.register(handle_gift_message_prompt, F.data.startswith('gift_msg:'))
    dp.callback_query.register(handle_gift_pay, F.data.startswith('gift_pay:'))
    dp.message.register(handle_gift_message_input, GiftPurchaseStates.waiting_for_message)
    dp.message.register(handle_gift_recipient_input, GiftPurchaseStates.waiting_for_recipient)
