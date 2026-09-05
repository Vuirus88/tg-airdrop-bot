"""Admin review commands and callback handlers."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.keyboards import (
    archive_list_keyboard,
    archive_menu_keyboard,
    open_in_x_keyboard,
    queue_review_keyboard,
    review_keyboard,
)
from config import settings
from db.database import get_session
from db.models import Draft, Project, ProjectStatus, ReviewRequest
from ingestion.scheduler import source_scanner
from publishing.dispatcher import publish_project
from publishing.state import (
    archive_project_for_review,
    claim_project_for_publication,
    finish_project_publication,
)
from services.ai_rework import rework_draft
from services.audit import add_audit_event
from services.image_rework import extract_image_title, requests_image_rework, requests_image_text_rework
from services.llm_draft import DraftResult
from services.media import telegram_photo
from services.project_image import discover_project_image
from services.project_link import discover_project_link
from services.review_queue import (
    adjacent_project_id,
    archived_projects,
    clear_archive,
    pending_projects,
    queue_position,
)
from services.social_card import generate_social_card
from services.health import collect_system_health
from services.telegram_cleanup import delete_messages, remember_message, tracked_message_ids

router = Router()


def _queue_review_text(project: Project, draft: Draft, position: tuple[int, int]) -> str:
    confidence_note = "сомнительный кандидат — проверьте источник" if (project.legitimacy_score or 0) < 6 else "кандидат прошел фильтр"
    text = (
        f"Новый кандидат: {project.legitimacy_score or 0:.1f}/10\n"
        f"Статус: {confidence_note}\n"
        f"Почему: {project.score_reasoning or 'не указано'}\n"
        f"Очередь: {position[0]}/{position[1]}\n\n"
        + draft.rendered_review_text()
    )
    return text if len(text) <= 4096 else text[:4090] + "\n..."


async def _present_queue_item(bot, chat_id: int, project_id: int | None = None) -> bool:
    """Render exactly one pending project and remember every Telegram message."""
    async with get_session() as session:
        projects = await pending_projects(session)
        if not projects:
            await bot.send_message(chat_id=chat_id, text="Очередь черновиков пуста.")
            return False
        target = next((item for item in projects if item.id == project_id), projects[0])
        position = queue_position(projects, target.id)
        if not position or not target.latest_draft():
            return False
        request = await session.scalar(select(ReviewRequest).where(ReviewRequest.project_id == target.id))
        if not request:
            request = ReviewRequest(project_id=target.id)
            session.add(request)
            await session.flush()
        old_ids = tracked_message_ids(request)
        if target.review_message_id:
            old_ids.append(target.review_message_id)
        await delete_messages(bot, chat_id, old_ids)
        request.message_ids_json = "[]"
        draft = target.latest_draft()
        if draft.image_path:
            try:
                photo = await bot.send_photo(
                    chat_id=chat_id,
                    photo=telegram_photo(draft.image_path),
                    caption=f"Черновик {position[0]}/{position[1]}: {target.name}",
                )
                remember_message(request, photo.message_id)
                if draft.image_source and draft.image_source.startswith("generated_social_card"):
                    draft.image_path = photo.photo[-1].file_id
                    draft.image_source = "telegram_file_id"
            except Exception:
                pass
        message = await bot.send_message(
            chat_id=chat_id,
            text=_queue_review_text(target, draft, position),
            reply_markup=queue_review_keyboard(target.id, *position),
        )
        remember_message(request, message.message_id)
        target.review_chat_id = chat_id
        target.review_message_id = message.message_id
        await session.commit()
        return True


async def _cleanup_current_queue_item(bot, chat_id: int, project_id: int) -> None:
    async with get_session() as session:
        project = await _load_project(session, project_id)
        request = await session.scalar(select(ReviewRequest).where(ReviewRequest.project_id == project_id))
        ids = tracked_message_ids(request) if request else []
        if project and project.review_message_id:
            ids.append(project.review_message_id)
        await delete_messages(bot, chat_id, ids)
        if request:
            request.message_ids_json = "[]"
        if project:
            project.review_message_id = None
            project.review_chat_id = None
        await session.commit()


async def initialize_review_queue(bot) -> None:
    """Collapse legacy multi-message reviews to one visible queue item."""
    async with get_session() as session:
        projects = await pending_projects(session)
        for project in projects:
            request = await session.scalar(
                select(ReviewRequest).where(ReviewRequest.project_id == project.id)
            )
            ids = tracked_message_ids(request) if request else []
            if project.review_message_id:
                ids.append(project.review_message_id)
            await delete_messages(bot, settings.ADMIN_USER_ID, ids)
            if request:
                request.message_ids_json = "[]"
            project.review_message_id = None
            project.review_chat_id = None
        await session.commit()
    await _present_queue_item(bot, settings.ADMIN_USER_ID)


@router.callback_query(F.data == "queue:noop")
async def on_queue_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("queue:prev:"))
@router.callback_query(F.data.startswith("queue:next:"))
async def on_queue_navigation(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    _, direction_name, raw_id = callback.data.split(":")
    current_id = int(raw_id)
    async with get_session() as session:
        projects = await pending_projects(session)
        target_id = adjacent_project_id(projects, current_id, -1 if direction_name == "prev" else 1)
    if not target_id:
        await callback.answer("Это крайний черновик в очереди.", show_alert=True)
        return
    await _cleanup_current_queue_item(callback.bot, callback.message.chat.id, current_id)
    await _present_queue_item(callback.bot, callback.message.chat.id, target_id)
    await callback.answer()


@router.callback_query(F.data == "queue:show")
async def on_queue_show(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await _present_queue_item(callback.bot, callback.message.chat.id)
    await callback.answer()


def _archive_text(projects: list[Project], kind: str) -> str:
    title = "Опубликованные проекты" if kind == "published" else "Удалённые проекты"
    if not projects:
        return f"{title}\n\nАрхив пуст."
    lines = [f"{title} ({len(projects)})", ""]
    for index, project in enumerate(projects[:20], start=1):
        lines.append(f"{index}. #{project.id} {project.name}")
        if project.project_url:
            lines.append(project.project_url)
    if len(projects) > 20:
        lines.append(f"\nПоказаны последние 20 из {len(projects)}.")
    return "\n".join(lines)


@router.callback_query(F.data == "archive:menu")
async def on_archive_menu(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text("Архивы черновиков:", reply_markup=archive_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("archive:list:"))
async def on_archive_list(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = callback.data.split(":")[-1]
    if kind not in {"published", "deleted"}:
        await callback.answer("Неизвестный архив.", show_alert=True)
        return
    async with get_session() as session:
        projects = await archived_projects(session, kind)
    await callback.message.edit_text(_archive_text(projects, kind), reply_markup=archive_list_keyboard(kind))
    await callback.answer()


@router.callback_query(F.data.startswith("archive:clear:"))
async def on_archive_clear(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    kind = callback.data.split(":")[-1]
    async with get_session() as session:
        removed = await clear_archive(session, kind)
        await session.commit()
    await callback.message.edit_text(
        f"Архив очищен. Удалено записей: {removed}.",
        reply_markup=archive_menu_keyboard(),
    )
    await callback.answer("Готово")


@router.message(Command("queue"))
async def on_queue_command(message: Message):
    if not _is_admin_message(message):
        return
    await _present_queue_item(message.bot, message.chat.id)


@router.message(Command("archives"))
async def on_archives_command(message: Message):
    if not _is_admin_message(message):
        return
    await message.answer("Архивы черновиков:", reply_markup=archive_menu_keyboard())
def _is_admin_message(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == settings.ADMIN_USER_ID)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return callback.from_user.id == settings.ADMIN_USER_ID


async def _load_project(session, project_id: int) -> Project | None:
    result = await session.execute(
        select(Project).options(selectinload(Project.drafts)).where(Project.id == project_id)
    )
    return result.scalar_one_or_none()


async def _queue_markup(project_id: int):
    async with get_session() as session:
        projects = await pending_projects(session)
    position = queue_position(projects, project_id)
    return queue_review_keyboard(project_id, *position) if position else review_keyboard(project_id)


@router.callback_query(F.data.startswith("approve:"))
async def on_approve(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return

    project_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            await callback.answer("Проект или черновик не найден.", show_alert=True)
            return
        if project.status == ProjectStatus.PUBLISHED:
            await callback.answer("Этот проект уже опубликован.", show_alert=True)
            return
        if not project.latest_draft().project_url:
            await callback.answer(
                "Ссылка на проект не найдена. Публикация заблокирована, чтобы не отправить неверный URL.",
                show_alert=True,
            )
            return
        review_request = await session.scalar(
            select(ReviewRequest).where(ReviewRequest.project_id == project.id)
        )
        cleanup_ids = tracked_message_ids(review_request) if review_request else []
        if project.review_message_id:
            cleanup_ids.append(project.review_message_id)

        claimed = await claim_project_for_publication(session, project.id)
        if not claimed:
            await callback.answer(
                "Этот пост уже обрабатывается или был опубликован.", show_alert=True
            )
            return
        await session.commit()
        add_audit_event(
            session,
            "telegram_approve_started",
            project_id=project.id,
            actor_type="telegram",
            actor_id=callback.from_user.id,
        )
        results = await publish_project(callback.bot, project, project.latest_draft())
        telegram_result = next(result for result in results if result.platform == "telegram")
        add_audit_event(
            session,
            "publish_completed",
            project_id=project.id,
            actor_type="telegram",
            actor_id=callback.from_user.id,
            success=telegram_result.success,
            detail="; ".join(
                f"{item.platform}: {item.error or 'ok'}" for item in results
            ),
        )
        await finish_project_publication(session, project.id, telegram_result.success)

        if callback.message:
            lines = ["Результат публикации:"]
            for result in results:
                marker = "✅" if result.success else "❌"
                detail = result.url or result.error or "готово"
                lines.append(f"{marker} {result.platform}: {detail}")
            x_result = next(result for result in results if result.platform == "x")
            fallback_keyboard = (
                open_in_x_keyboard(project.latest_draft().twitter_text)
                if not x_result.success and project.latest_draft().twitter_text
                else None
            )
            result_text = "\n".join(lines)
            fallback_image = project.latest_draft().image_path
            if fallback_keyboard and fallback_image:
                try:
                    await callback.bot.send_photo(
                        chat_id=callback.message.chat.id,
                        photo=telegram_photo(fallback_image),
                        caption=(
                            result_text
                            + "\n\nФото для ручной публикации в X. Откройте X кнопкой ниже, "
                            "затем прикрепите это изображение."
                        )[:1024],
                        reply_markup=fallback_keyboard,
                    )
                except Exception:
                    await callback.message.answer(result_text, reply_markup=fallback_keyboard)
            else:
                await callback.message.answer(result_text, reply_markup=fallback_keyboard)
            await delete_messages(
                callback.bot,
                callback.message.chat.id,
                cleanup_ids + [callback.message.message_id],
            )
            if review_request:
                review_request.message_ids_json = "[]"
                await session.commit()
            await _present_queue_item(callback.bot, callback.message.chat.id)

    if telegram_result.success:
        await callback.answer("Опубликовано в Telegram.")
    else:
        await callback.answer("Telegram не опубликовал пост. Смотрите ошибку ниже.", show_alert=True)


@router.callback_query(F.data.startswith("delete:"))
async def on_delete(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    project_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project:
            await callback.answer("Проект не найден.", show_alert=True)
            return
        archived = await archive_project_for_review(session, project.id)
        if not archived:
            await callback.answer(
                "Этот проект уже опубликован, удален или обрабатывается.",
                show_alert=True,
            )
            return
        add_audit_event(
            session,
            "project_deleted",
            project_id=project.id,
            actor_type="telegram",
            actor_id=callback.from_user.id,
        )
        await session.commit()
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
            request = await session.scalar(
                select(ReviewRequest).where(ReviewRequest.project_id == project.id)
            )
            cleanup_ids = tracked_message_ids(request) if request else []
            if project.review_message_id:
                cleanup_ids.append(project.review_message_id)
            await delete_messages(
                callback.bot,
                callback.message.chat.id,
                cleanup_ids + [callback.message.message_id],
            )
            await callback.message.answer(f"Проект #{project.id} удален и архивирован.")
            await _present_queue_item(callback.bot, callback.message.chat.id)
    await callback.answer("Удалено.")


@router.callback_query(F.data.startswith("rework:"))
async def on_rework(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    project_id = int(callback.data.split(":")[1])
    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or project.status not in {ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED}:
            await callback.answer("Этот проект больше не ожидает Rework.", show_alert=True)
            return
        request = await session.scalar(
            select(ReviewRequest).where(ReviewRequest.project_id == project_id)
        )
        if not request:
            request = ReviewRequest(project_id=project_id)
            session.add(request)
            await session.flush()
        request.status = "awaiting_feedback"
        request.feedback = None
        request.resolved_at = None
        prompt = None
        if callback.message:
            prompt = await callback.message.reply(
            "Ответьте на это сообщение и напишите, что изменить в черновике.\n"
            f"(project #{project_id})"
            )
            request.prompt_chat_id = prompt.chat.id
            request.prompt_message_id = prompt.message_id
            remember_message(request, prompt.message_id)
        add_audit_event(
            session,
            "rework_requested",
            project_id=project_id,
            actor_type="telegram",
            actor_id=callback.from_user.id,
        )
        await session.commit()
    await callback.answer()


@router.callback_query(F.data.startswith("regen_image:"))
async def on_regenerate_image(callback: CallbackQuery):
    if not _is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    project_id = int(callback.data.split(":")[1])
    await callback.answer("Генерирую новую картинку...")

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if (
            not project
            or not project.latest_draft()
            or project.status not in {ProjectStatus.PENDING_REVIEW, ProjectStatus.DRAFTED}
        ):
            if callback.message:
                await callback.message.answer(
                    "Картинку можно менять только у черновика, который ожидает проверки."
                )
            return
        previous_draft = project.latest_draft()
        new_version = previous_draft.version + 1
        official_image = await discover_project_image(project.source_url)
        regeneration_prompt = previous_draft.image_prompt or (
            f"{project.category} opportunity, red and teal cinematic environment"
        )
        social_card = await generate_social_card(
            name=project.name,
            category=project.category,
            chain=project.chain,
            instructions=previous_draft.instructions,
            official_image_url=official_image.url if official_image else None,
            image_prompt=regeneration_prompt,
            project_url=project.project_url,
            generation_key=f"project-{project.id}-v{new_version}",
        )
        if not social_card:
            if callback.message:
                await callback.message.answer(
                    "Не удалось создать новую картинку. Предыдущая версия сохранена.",
                    reply_markup=await _queue_markup(project.id),
                )
            return

        new_draft = Draft(
            project_id=project.id,
            version=new_version,
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_path=social_card.path,
            image_source=social_card.source,
            image_prompt=regeneration_prompt,
            source_url=previous_draft.source_url or project.source_url,
            project_url=previous_draft.project_url or project.project_url,
            rework_feedback="Regenerate image button",
        )
        project.drafts.append(new_draft)
        add_audit_event(
            session,
            "image_regenerated",
            project_id=project.id,
            actor_type="telegram",
            actor_id=callback.from_user.id,
            detail=f"draft_version={new_version}; provider={social_card.source}",
        )
        await session.commit()

        if callback.message:
            provider = (
                "Cloudflare Workers AI"
                if social_card.source == "generated_social_card_cloudflare"
                else "локальный резервный генератор"
            )
            photo = await callback.bot.send_photo(
                chat_id=callback.message.chat.id,
                photo=telegram_photo(new_draft.image_path),
                caption=f"Новая картинка для версии {new_version}, источник: {provider}",
            )
            new_draft.image_path = photo.photo[-1].file_id
            new_draft.image_source = "telegram_file_id"
            await session.commit()
            await callback.message.answer(
                f"Создана версия {new_version}. Текст сохранён без изменений.",
                reply_markup=await _queue_markup(project.id),
            )


@router.message(F.reply_to_message, F.text)
async def on_feedback_reply(message: Message):
    if not _is_admin_message(message):
        return
    prompt_message_id = message.reply_to_message.message_id
    async with get_session() as session:
        pending = await session.scalar(
            select(ReviewRequest).where(
                ReviewRequest.prompt_chat_id == message.chat.id,
                ReviewRequest.prompt_message_id == prompt_message_id,
                ReviewRequest.status == "awaiting_feedback",
            )
        )
    if not pending:
        return
    project_id = pending.project_id

    async with get_session() as session:
        project = await _load_project(session, project_id)
        if not project or not project.latest_draft():
            await message.answer("Проект или черновик не найден.")
            return
        previous_draft = project.latest_draft()
        if not project.project_url:
            project.project_url = await discover_project_link(
                project.source_url, project.raw_data or "", project.name
            )
        previous = DraftResult(
            title=previous_draft.title,
            summary=previous_draft.summary,
            instructions=previous_draft.instructions,
            potential_reward=previous_draft.potential_reward,
            risk_note=previous_draft.risk_note,
            twitter_text=previous_draft.twitter_text,
            image_prompt=previous_draft.image_prompt,
        )
        try:
            new_result, rework_provider = await rework_draft(
                project.name,
                project.raw_data,
                project.chain,
                project.source_url,
                project.project_url,
                previous,
                message.text,
            )
        except Exception as exc:
            await session.rollback()
            error_message = await message.answer(
                "Groq и резервный Gemini сейчас недоступны, поэтому переработка не выполнена. "
                "Текущий черновик сохранён без изменений и ожидает дальнейших действий.\n\n"
                f"Причина: {str(exc)[:500]}",
                reply_markup=await _queue_markup(project_id),
            )
            async with get_session() as update_session:
                request = await update_session.get(ReviewRequest, pending.id)
                if request:
                    request.status = "awaiting_feedback"
                    request.feedback = message.text
                    remember_message(request, error_message.message_id)
                    await update_session.commit()
            return

        request = await session.get(ReviewRequest, pending.id)
        cleanup_ids = tracked_message_ids(request) if request else []
        if project.review_message_id:
            cleanup_ids.append(project.review_message_id)
        if request:
            request.status = "completed"
            request.feedback = message.text
            request.resolved_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        image_requested = requests_image_rework(message.text)
        image_title = extract_image_title(message.text) if requests_image_text_rework(message.text) else None
        social_card = None
        if image_requested:
            official_image = await discover_project_image(project.source_url)
            social_card = await generate_social_card(
                name=image_title or project.name,
                category=project.category,
                chain=project.chain,
                instructions=new_result.instructions,
                official_image_url=official_image.url if official_image else None,
                image_prompt=new_result.image_prompt,
                project_url=project.project_url,
                generation_key=f"project-{project.id}-v{previous_draft.version + 1}",
            )
        new_draft = Draft(
            project_id=project.id,
            version=previous_draft.version + 1,
            title=new_result.title,
            summary=new_result.summary,
            instructions=new_result.instructions,
            potential_reward=new_result.potential_reward,
            risk_note=new_result.risk_note,
            twitter_text=new_result.twitter_text,
            image_path=social_card.path if social_card else previous_draft.image_path,
            image_source=social_card.source if social_card else previous_draft.image_source,
            image_prompt=(
                new_result.image_prompt
                if image_requested and social_card
                else previous_draft.image_prompt
            ),
            source_url=project.source_url,
            project_url=project.project_url,
            rework_feedback=message.text,
        )
        project.drafts.append(new_draft)
        add_audit_event(
            session,
            "rework_completed",
            project_id=project.id,
            actor_type="telegram",
            actor_id=message.from_user.id if message.from_user else None,
            detail=f"provider={rework_provider}; image_requested={image_requested}",
        )
        await session.commit()
        await delete_messages(message.bot, message.chat.id, cleanup_ids)
        if image_requested and new_draft.image_path:
            try:
                photo = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=telegram_photo(new_draft.image_path),
                    caption=f"Новая social card для версии {new_draft.version}",
                )
                if social_card:
                    new_draft.image_path = photo.photo[-1].file_id
                    new_draft.image_source = "telegram_file_id"
            except Exception:
                pass
        new_review_message = await message.answer(
            f"Переработано через {rework_provider}, версия {new_draft.version}\n"
            f"Изображение: {'создано заново' if image_requested and social_card else 'сохранено без изменений'}\n\n"
            f"{new_draft.rendered_review_text()}",
            reply_markup=await _queue_markup(project.id),
        )
        project.review_chat_id = new_review_message.chat.id
        project.review_message_id = new_review_message.message_id
        if request:
            request.message_ids_json = "[]"
        await session.commit()


@router.message(Command("scan_now"))
async def on_scan_now(message: Message):
    if not _is_admin_message(message):
        await message.answer("Этот бот доступен только администратору.")
        return
    await message.answer("Сканирую источники. Облачная AI-проверка может занять несколько минут...")
    summary = await source_scanner.scan_once()
    await message.answer(
        "Сканирование завершено.\n"
        f"Найдено сигналов: {summary['collected']}\n"
        f"Поставлено в очередь: {summary.get('queued', 0)}\n"
        f"Отправлено на проверку: {summary['sent_for_review']}\n"
        f"Отфильтровано: {summary['filtered']}\n"
        f"Дубликаты/уже обработаны: {summary['duplicates']}\n"
        f"Ошибки: {summary['errors']}\n"
        f"Обработано через Groq: {summary['groq']}\n"
        f"Обработано локальным режимом без AI: {summary['fallback']}"
    )
    report_lines = ["", "Диагностика источников:"]
    for report in summary.get("source_reports", []):
        if report["error"]:
            report_lines.append(f"❌ {report['source']}: {report['error']}")
            continue
        reasons = ", ".join(
            f"{key}={value}" for key, value in report["reason_counts"].items()
        ) or "без отказов"
        report_lines.append(
            f"✅ {report['source']}: записей {report['entries']}, "
            f"кандидатов {report['candidates']}, отклонено {report['rejected']} "
            f"({reasons})"
        )
    await message.answer("\n".join(report_lines))


@router.message(Command("status"))
@router.message(Command("channel_status"))
async def on_system_status(message: Message):
    if not _is_admin_message(message):
        return
    progress = await message.answer("Проверяю источники и подключения...")
    health = await collect_system_health(message.bot)

    lines = [
        "Статус системы",
        "",
        f"1. Источники: {health.working_sources}/{len(health.sources)} работают",
    ]
    for source in health.sources:
        marker = "✅" if source.working else "❌"
        lines.append(f"{marker} {source.name}: {source.detail}")

    lines.extend(
        [
            "",
            f"2. Telegram: {'✅' if health.telegram.working else '❌'} {health.telegram.detail}",
            f"3. X/Twitter: {'✅' if health.x.working else '❌'} {health.x.detail}",
            f"4. Groq (основной AI): {'✅' if health.groq.working else '❌'} {health.groq.detail}",
            f"5. Gemini (резерв): {'✅' if health.gemini.working else '❌'} {health.gemini.detail}",
            f"6. Cloudflare Images: {'✅' if health.cloudflare.working else '❌'} {health.cloudflare.detail}",
            "",
            "7. Рекомендации:",
        ]
    )
    lines.extend(f"• {recommendation}" for recommendation in health.recommendations)
    await progress.edit_text("\n".join(lines))
