"""${message}

Revision: ${up_revision}
Previous: ${down_revision | comma,n}
Created:  ${create_date}
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
% if upgrades:
    ${upgrades}
% else:
    # Write your migration here. Data migration example:
    #
    #     posts = sa.table("posts", sa.column("title", sa.String), sa.column("slug", sa.String))
    #     op.execute(posts.update().values(slug=sa.func.lower(posts.c.title)))
    pass
% endif


def downgrade() -> None:
% if downgrades:
    ${downgrades}
% else:
    pass
% endif
