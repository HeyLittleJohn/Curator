"""added options_quotes raw table

Revision ID: 0a7cb5dcb2f5
Revises: 20b9727bdd63
Create Date: 2024-06-11 17:27:56.603093

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0a7cb5dcb2f5'
down_revision = '20b9727bdd63'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('options_quotes', 'sequence_number',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=True)
    op.alter_column('options_quotes', 'sip_timestamp',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               existing_nullable=False)
    op.create_unique_constraint(None, 'options_quotes', ['id'])
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, 'options_quotes', type_='unique')
    op.alter_column('options_quotes', 'sip_timestamp',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
    op.alter_column('options_quotes', 'sequence_number',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=True)
    # ### end Alembic commands ###
