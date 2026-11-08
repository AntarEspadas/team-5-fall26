"""Apply and lift bans while preserving offense history and admin attribution."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import User, UserBan

FIRST_BAN_DURATION = timedelta(hours=120)


def get_active_ban(user, *, at=None):
    """Return the user's active ban, or None, using current database state."""
    if not user.is_authenticated or user.pk is None:
        return None
    if at is None:
        at = timezone.now()
    return (
        UserBan.objects.filter(user=user, lifted_at__isnull=True)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=at))
        .first()
    )


def _authorized_admin(actor):
    """Reload permissions so stale user objects cannot authorize moderation."""
    if not actor.is_authenticated or actor.pk is None:
        raise PermissionDenied("Only user-management admins can manage bans.")
    admin = User.objects.filter(pk=actor.pk, is_active=True, is_staff=True).first()
    if admin is None or not admin.has_perm("accounts.change_user"):
        raise PermissionDenied("Only user-management admins can manage bans.")
    return admin


def _check_target(user):
    if user.is_staff or user.is_superuser:
        raise PermissionDenied("Staff and superusers cannot be banned.")


@transaction.atomic
def apply_ban(actor, user):
    """Return (ban, created); an existing active ban never adds an offense.

    Lock the user, including when no ban exists yet, so concurrent admins
    cannot create two offenses for the same incident. Time starts after
    acquiring the lock. Only the first offense receives a finite expiration.
    """
    admin = _authorized_admin(actor)
    target = User.objects.select_for_update().get(pk=user.pk)
    _check_target(target)
    now = timezone.now()
    active_ban = get_active_ban(target, at=now)
    if active_ban is not None:
        return active_ban, False

    last_offense = target.bans.values_list("offense_number", flat=True).first() or 0
    offense_number = last_offense + 1
    ban = UserBan.objects.create(
        user=target,
        offense_number=offense_number,
        issued_by=admin,
        started_at=now,
        expires_at=now + FIRST_BAN_DURATION if offense_number == 1 else None,
    )
    return ban, True


@transaction.atomic
def lift_ban(actor, ban):
    """Return (ban, lifted), changing only the specified ban if still active.

    Taking a specific ban prevents a repeated or stale request from lifting
    a later offense. Use the same user lock as apply_ban to serialize changes.
    """
    admin = _authorized_admin(actor)
    target = User.objects.select_for_update().get(pk=ban.user_id)
    _check_target(target)
    current = UserBan.objects.get(pk=ban.pk, user=target)
    if not current.is_active:
        return current, False

    current.lifted_at = timezone.now()
    current.lifted_by = admin
    current.save(update_fields=["lifted_at", "lifted_by"])
    return current, True
