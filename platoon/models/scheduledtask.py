from datetime import datetime, timedelta
from traceback import format_exc

from scheme import UTC, current_timestamp
from spire.schema import *
from spire.support.logs import LogHelper

from platoon.constants import *
from platoon.models.task import *

__all__ = ('ScheduledTask',)

log = LogHelper('platoon')
schema = Schema('platoon')

class ScheduledTask(Task):
    """A scheduled task."""

    class meta:
        polymorphic_identity = 'scheduled'
        schema = schema
        tablename = 'scheduled_task'

    task_id = ForeignKey('task.id', nullable=False, primary_key=True, ondelete='CASCADE')
    status = Enumeration('pending executing retrying aborted completed failed',
        nullable=False, default='pending')
    occurrence = DateTime(nullable=False, timezone=True)
    parent_id = ForeignKey('recurring_task.task_id', ondelete='CASCADE')
    parameters = Serialized()

    parent = relationship('RecurringTask', primaryjoin='RecurringTask.task_id==ScheduledTask.parent_id',
        cascade='all')
    executions = relationship(TaskExecution, backref='task', order_by='TaskExecution.attempt',
        cascade='all,delete-orphan', passive_deletes=True)

    @classmethod
    def create(cls, session, tag, action, status='pending', occurrence=None,
            failed_action=None, completed_action=None, description=None,
            retry_backoff=None, retry_limit=2, retry_timeout=300):

        occurrence = occurrence or datetime.now(UTC)
        task = ScheduledTask(tag=tag, status=status, description=description,
            occurrence=occurrence, retry_backoff=retry_backoff,
            retry_limit=retry_limit, retry_timeout=retry_timeout)

        task.action = TaskAction.polymorphic_create(action)
        if failed_action:
            task.failed_action = TaskAction.polymorphic_create(failed_action)
        if completed_action:
            task.completed_action = TaskAction.polymorphic_create(completed_action)

        session.add(task)
        return task

    def execute(self, session):
        parent = None
        if self.parent_id:
            parent = RecurringTask.load(session, id=self.parent_id, lockmode='update')

        execution = TaskExecution(task_id=self.id, attempt=len(self.executions) + 1)
        session.add(execution)

        execution.started = datetime.now(UTC)
        try:
            status, execution.result = self.action.execute(self, session)
        except Exception, exception:
            status = FAILED
            execution.result = format_exc()

        execution.completed = datetime.now(UTC)
        if status == COMPLETED:
            self.status = execution.status = 'completed'
            log('info', '%s completed (attempt %d)', repr(self), execution.attempt)
            log('debug', 'result for %s:\n%s', repr(self), execution.result)
            if self.completed_action_id:
                session.add(Task(tag='%s-completed' % self.tag, occurrence=datetime.now(UTC),
                    action_id=self.completed_action_id))
        elif execution.attempt == (self.retry_limit + 1):
            self.status = execution.status = 'failed'
            log('error', '%s failed (attempt %d), aborting', repr(self), execution.attempt)
            log('debug', 'result for %s:\n%s', repr(self), execution.result)
            if self.failed_action_id:
                session.add(Task(tag='%s-failed' % self.tag, occurrence=datetime.now(UTC),
                    action_id=self.failed_action_id))
        else:
            execution.status = 'failed'
            self.status = 'retrying'
            self.occurrence = self._calculate_retry(execution)
            if status == FAILED:
                log('warning', '%s failed (attempt %d), retrying', repr(self), execution.attempt)
            else:
                log('info', '%s not yet complete (attempt %s), retrying',
                    repr(self), execution.attempt)
            log('debug', 'result for %s:\n%s', repr(self), execution.result)

        if parent:
            parent.reschedule(session, self.occurrence)

    @classmethod
    def purge(cls, session, lifetime):
        delta = current_timestamp() - timedelta(days=lifetime)
        session.query(cls).filter(cls.status == 'completed', cls.occurrence < delta).delete()

    @classmethod
    def spawn(cls, template, occurrence=None, **params):
        occurrence = occurrence or datetime.now(UTC)
        return cls(tag=template.tag, status='pending', description=template.description,
            occurrence=occurrence, retry_backoff=template.retry_backoff,
            retry_limit=template.retry_limit, retry_timeout=template.retry_timeout,
            action_id=template.action_id, failed_action_id=template.failed_action_id,
            completed_action_id=template.completed_action_id, **params)

    def update(self, session, data):
        raise NotImplemented()

    def _calculate_retry(self, execution):
        timeout = self.retry_timeout
        if self.retry_backoff is not None:
            timeout *= (self.retry_backoff ** execution.attempt)
        return datetime.now(UTC) + timedelta(seconds=timeout)