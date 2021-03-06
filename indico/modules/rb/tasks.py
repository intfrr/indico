# This file is part of Indico.
# Copyright (C) 2002 - 2018 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, unicode_literals

from datetime import date, datetime
from itertools import groupby
from operator import attrgetter

from celery.schedules import crontab
from sqlalchemy.orm import contains_eager

from indico.core.celery import celery
from indico.core.config import config
from indico.core.db import db
from indico.modules.rb import logger, rb_settings
from indico.modules.rb.models.reservation_occurrences import ReservationOccurrence
from indico.modules.rb.models.reservations import RepeatFrequency, Reservation
from indico.modules.rb.models.rooms import Room
from indico.modules.rb.notifications.reservation_occurrences import notify_upcoming_occurrences
from indico.modules.rb.notifications.reservations import notify_about_finishing_bookings
from indico.util.console import cformat


def _make_occurrence_date_filter(date_column, default_values, room_columns):
    notification_before = db.case({RepeatFrequency.WEEK.value: room_columns['weekly'],
                                   RepeatFrequency.MONTH.value: room_columns['monthly']},
                                  else_=room_columns['default'], value=Reservation.repeat_frequency)
    notification_before_default = db.case({RepeatFrequency.WEEK.value: default_values['weekly'],
                                           RepeatFrequency.MONTH.value: default_values['monthly']},
                                          else_=default_values['default'], value=Reservation.repeat_frequency)
    notification_before_days = db.func.coalesce(notification_before, notification_before_default)
    days_until = db.cast(date_column, db.Date) - date.today()
    return days_until == notification_before_days


def _print_occurrences(user, occurrences, _defaults={}, _overrides={}):
    if not _defaults or not _overrides:
        _defaults.update({RepeatFrequency.WEEK: rb_settings.get('notification_before_days_weekly'),
                          RepeatFrequency.MONTH: rb_settings.get('notification_before_days_monthly'),
                          RepeatFrequency.NEVER: rb_settings.get('notification_before_days'),
                          RepeatFrequency.DAY: rb_settings.get('notification_before_days')})
        _overrides.update({RepeatFrequency.WEEK: lambda r: r.notification_before_days_weekly,
                           RepeatFrequency.MONTH: lambda r: r.notification_before_days_monthly,
                           RepeatFrequency.NEVER: lambda r: r.notification_before_days,
                           RepeatFrequency.DAY: lambda r: r.notification_before_days})
    print(cformat('%{grey!}*** {} ({}) ***').format(user.full_name, user.email))
    for occ in occurrences:
        default = _defaults[occ.reservation.repeat_frequency]
        override = _overrides[occ.reservation.repeat_frequency](occ.reservation.room)
        days = default if override is None else override
        days_until = (occ.start_dt.date() - date.today()).days
        print(cformat('  * %{yellow}{}%{reset} %{green}{:5}%{reset} {} {} {} \t %{blue!}{}%{reset} {} ({})').format(
            occ.start_dt.date(), occ.reservation.repeat_frequency.name,
            days,
            default if override is not None and override != default else ' ',
            days_until,
            occ.reservation.id,
            occ.reservation.room.full_name,
            occ.reservation.room.id
        ))


def _notify_occurrences(user, occurrences):
    notify_upcoming_occurrences(user, occurrences)
    for occ in occurrences:
        occ.notification_sent = True
        if occ.reservation.repeat_frequency == RepeatFrequency.DAY:
            future_occurrences_query = (occ.reservation.occurrences
                                        .filter(ReservationOccurrence.start_dt >= datetime.now()))
            future_occurrences_query.update({'notification_sent': True})


@celery.periodic_task(name='roombooking_occurrences', run_every=crontab(minute='15', hour='8'))
def roombooking_occurrences(debug=False):
    if not config.ENABLE_ROOMBOOKING:
        logger.info('Notifications not sent because room booking is disabled')
        return
    if not rb_settings.get('notifications_enabled'):
        logger.info('Notifications not sent because they are globally disabled')
        return

    defaults = {
        'default': rb_settings.get('notification_before_days'),
        'weekly': rb_settings.get('notification_before_days_weekly'),
        'monthly': rb_settings.get('notification_before_days_monthly')
    }

    room_columns = {
        'default': Room.notification_before_days,
        'weekly': Room.notification_before_days_weekly,
        'monthly': Room.notification_before_days_monthly
    }

    occurrences = (ReservationOccurrence.query
                   .join(ReservationOccurrence.reservation)
                   .join(Reservation.room)
                   .filter(Room.is_active,
                           Room.notifications_enabled,
                           Reservation.is_accepted,
                           Reservation.booked_for_id.isnot(None),
                           ReservationOccurrence.is_valid,
                           ReservationOccurrence.start_dt >= datetime.now(),
                           ~ReservationOccurrence.notification_sent,
                           _make_occurrence_date_filter(ReservationOccurrence.start_dt, defaults, room_columns))
                   .order_by(Reservation.booked_for_id, ReservationOccurrence.start_dt, Room.id)
                   .options(contains_eager('reservation').contains_eager('room'))
                   .all())

    for user, user_occurrences in groupby(occurrences, key=attrgetter('reservation.booked_for_user')):
        user_occurrences = list(user_occurrences)
        if debug:
            _print_occurrences(user, user_occurrences)
        else:
            _notify_occurrences(user, user_occurrences)
    if not debug:
        db.session.commit()


@celery.periodic_task(name='roombooking_end_notifications', run_every=crontab(minute='0', hour='8'))
def roombooking_end_notifications():
    if not config.ENABLE_ROOMBOOKING:
        logger.info('Notifications not sent because room booking is disabled')
        return
    if not rb_settings.get('end_notifications_enabled'):
        logger.info('Notifications not sent because they are globally disabled')
        return

    defaults = {
        'default': rb_settings.get('end_notification_daily'),
        'weekly': rb_settings.get('end_notification_weekly'),
        'monthly': rb_settings.get('end_notification_monthly')
    }

    room_columns = {
        'default': Room.end_notification_daily,
        'weekly': Room.end_notification_weekly,
        'monthly': Room.end_notification_monthly
    }

    reservations = (Reservation.query
                    .join(Reservation.room)
                    .filter(Room.is_active,
                            Room.end_notifications_enabled,
                            Reservation.is_accepted,
                            Reservation.end_dt >= datetime.now(),
                            Reservation.repeat_frequency != RepeatFrequency.NEVER,
                            ~Reservation.end_notification_sent,
                            _make_occurrence_date_filter(Reservation.end_dt, defaults, room_columns))
                    .order_by(Reservation.booked_for_id, Reservation.start_dt, Room.id)
                    .all())

    for user, user_reservations in groupby(reservations, key=attrgetter('booked_for_user')):
        user_reservations = list(user_reservations)
        notify_about_finishing_bookings(user, list(user_reservations))
        for user_reservation in user_reservations:
            user_reservation.end_notification_sent = True

    db.session.commit()
