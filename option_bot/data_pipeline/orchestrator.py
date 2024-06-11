from datetime import datetime

from data_pipeline.download import (
    download_options_contracts,
    download_options_prices,
    download_options_quotes,
    download_options_snapshots,
    download_stock_metadata,
    download_stock_prices,
)
from data_pipeline.uploader import (
    upload_options_contracts,
    upload_options_prices,
    upload_options_quotes,
    upload_options_snapshots,
    upload_stock_metadata,
    upload_stock_prices,
)
from db_tools.queries import delete_stock_ticker
from db_tools.utils import generate_o_ticker_lookup, pull_tickers_from_db

from option_bot.proj_constants import log


async def import_all(tickers: list, start_date: datetime, end_date: datetime, months_hist: int):
    """this is THE trigger function. It will do the following:

    1. Download all metadata and prices for both stocks and options
    2. It will clean that data
    3. It will upload that data to the database

    The goal is for this to be able to create the process pools required for each step.

    May add toggle if "refreshing" the data or pulling everything fresh.
    As of now, refreshing won't change anything. Pull all history everytime.
    """
    # Download and upload metadata

    await download_stock_metadata(tickers=[], all_=True)
    await upload_stock_metadata(tickers=[], all_=True)

    # Get ticker_id_lookup from db
    ticker_lookup = await pull_tickers_from_db(tickers, all_=True)

    # Download and upload underlying stock prices
    await download_stock_prices(ticker_lookup, start_date, end_date)
    await upload_stock_prices(ticker_lookup)

    # Download and upload options contract data
    await download_options_contracts(ticker_id_lookup=ticker_lookup, months_hist=months_hist)
    await upload_options_contracts(ticker_lookup, months_hist=months_hist)

    # Download and upload current snapshot of options contracts
    o_tickers = await generate_o_ticker_lookup(tickers, all_=True, unexpired=True)

    await download_options_snapshots(list(o_tickers.values()))
    await upload_options_snapshots(o_tickers)

    # Download and upload options prices data
    o_tickers = await generate_o_ticker_lookup(tickers, all_=True)

    await download_options_prices(o_tickers=list(o_tickers.values()), months_hist=months_hist)
    await upload_options_prices(o_tickers)

    # Download and upload quotes
    await download_options_quotes(o_tickers=list(o_tickers.values()), months_hist=months_hist)
    # await upload_options_quotes(o_tickers)


async def import_partial(
    partial: list[int], tickers: list, start_date: datetime, end_date: datetime, months_hist: int
):
    """This will download, clean, and upload data for the components specified in `partial`
    This is meant to be used on an adhoc basis to fill in data gaps or backfill changes
    """

    all_ = True if len(tickers) == 0 else False
    ticker_lookup = None
    o_tickers = None

    if 1 in partial:  # stock metadata
        await download_stock_metadata(tickers=tickers, all_=all_)
        await upload_stock_metadata(tickers=tickers, all_=all_)

    if 2 in partial:  # stock prices
        ticker_lookup = await pull_tickers_from_db(tickers, all_)
        await download_stock_prices(ticker_lookup, start_date, end_date)
        await upload_stock_prices(ticker_lookup)

    if 3 in partial:  # options contracts
        if not ticker_lookup:
            ticker_lookup = await pull_tickers_from_db(tickers, all_)
        await download_options_contracts(ticker_id_lookup=ticker_lookup, months_hist=months_hist)
        await upload_options_contracts(ticker_lookup, months_hist=months_hist)

    if 4 in partial:  # options prices
        o_tickers = await generate_o_ticker_lookup(tickers, all_=all_)
        await download_options_prices(o_tickers=list(o_tickers.values()), months_hist=months_hist)
        await upload_options_prices(o_tickers)

    if 5 in partial:  # snapshots
        if not o_tickers:
            o_tickers = await generate_o_ticker_lookup(tickers, all_=all_, unexpired=True)
        await download_options_snapshots(list(o_tickers.values()))
        await upload_options_snapshots(o_tickers)

    if 6 in partial:  # quotes
        if not o_tickers:
            o_tickers = await generate_o_ticker_lookup(tickers, all_=all_)
        await download_options_quotes(o_tickers=list(o_tickers.values()), months_hist=months_hist)
        await upload_options_quotes(o_tickers)


async def remove_tickers_from_universe(tickers: list[str]):
    """Deletes the ticker from the stock ticker table which cascades and deletes all associated data."""
    for ticker in tickers:
        log.info(f"deleting ticker {ticker}")
        await delete_stock_ticker(ticker)
        log.info(f"ticker {ticker} successfully deleted")
