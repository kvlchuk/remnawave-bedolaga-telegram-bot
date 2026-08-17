"""Меню "Начать" — точка входа для пользователей без активной подписки.

Показывает подменю: (опционально) Тестовая подписка / Купить подписку /
Подарить подписку / Ввести промокод. Подарок пока временная заглушка —
полноценная покупка подарка прямо в боте будет добавлена отдельным шагом
(backend для неё уже готов, см. app.services.guest_purchase_service).
"""

import structlog
from aiogram import Dispatcher, F, types
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts


logger = structlog.get_logger(__name__)

START_MENU_TRIAL_ICON = '5258185631355378853'  # ⭐️ (заглушка, поменяем при желании)
START_MENU_BUY_ICON = '5359719332542718652'  # 💎
START_MENU_GIFT_ICON = '6032644646587338669'  # 🎁
START_MENU_PROMOCODE_ICON = '5296348778012361146'  # 🏷
START_MENU_BACK_ICON = '5258236805890710909'  # ⬅️


def _should_show_trial(user: User) -> bool:
    """Триал показываем только тем, кто им никогда не пользовался и не платил."""
    if settings.TRIAL_DURATION_DAYS <= 0 or settings.TRIAL_DISABLED_FOR == 'all':
        return False
    if settings.is_trial_disabled_for_user(getattr(user, 'auth_type', None)):
        return False
    try:
        if user.is_trial_already_used():
            return False
    except Exception as error:
        logger.debug('Не удалось проверить доступность триала для меню Начать', error=str(error))
        return False
    return True


def get_start_menu_keyboard(texts, show_trial: bool) -> InlineKeyboardMarkup:
    keyboard = []

    if show_trial:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=texts.t('START_MENU_TRIAL', 'Бесплатная подписка на 3 дня'),
                    icon_custom_emoji_id=START_MENU_TRIAL_ICON,
                    callback_data='menu_trial',
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('START_MENU_BUY', 'Купить подписку'),
                icon_custom_emoji_id=START_MENU_BUY_ICON,
                callback_data='menu_buy',
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('START_MENU_GIFT', 'Подарить подписку'),
                icon_custom_emoji_id=START_MENU_GIFT_ICON,
                callback_data='gift_purchase_start',
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('START_MENU_PROMOCODE', 'Ввести промокод'),
                icon_custom_emoji_id=START_MENU_PROMOCODE_ICON,
                callback_data='menu_promocode',
            )
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                text=texts.t('BACK', 'Назад'),
                icon_custom_emoji_id=START_MENU_BACK_ICON,
                callback_data='back_to_menu',
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def handle_start_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    texts = get_texts(db_user.language)
    show_trial = _should_show_trial(db_user)

    await callback.message.edit_text(
        texts.t('START_MENU_TITLE', '<b>Начать</b>\n\nВыберите действие:'),
        reply_markup=get_start_menu_keyboard(texts, show_trial),
    )
    await callback.answer()


async def handle_gift_purchase_stub(callback: types.CallbackQuery, db_user: User) -> None:
    """Временная заглушка — полноценная покупка подарка будет добавлена отдельно."""
    texts = get_texts(db_user.language)
    await callback.answer(
        texts.t('GIFT_PURCHASE_COMING_SOON', '🎁 Покупка подарка в боте скоро появится!'),
        show_alert=True,
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_start_menu, F.data == 'menu_start')
    dp.callback_query.register(handle_gift_purchase_stub, F.data == 'gift_purchase_start')
