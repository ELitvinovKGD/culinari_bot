"""extra dishes from lavka

Миграция c4e1a7d2b9f0, создана 2026-11-26 16:30.
Предыдущая миграция: 8f3772518b16

«➕ Дополнительные блюда»: позиция вне лавки помнит, из какой лавки пришла.

upgrade()   — применить изменение (alembic upgrade head)
downgrade() — откатить его (alembic downgrade -1)
"""

from alembic import op
import sqlalchemy as sa


revision = 'c4e1a7d2b9f0'
down_revision = '8f3772518b16'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Новая колонка пустая (NULL) у всех существующих строк — это и правильно:
    # всё, что уже вне лавки, — обычные блюда, а не порции из лавки.
    op.add_column('issue_items', sa.Column('extra_from_issue_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('issue_items', 'extra_from_issue_id')
