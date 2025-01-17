from datetime import datetime

from aiomultiprocess import Pool
from data_pipeline.exceptions import (
    InvalidArgs,
    ProjBaseException,
    ProjClientConnectionError,
    ProjClientResponseError,
    ProjIndexError,
    ProjTimeoutError,
)
from data_pipeline.polygon_utils import (
    CurrentContractSnapshot,
    HistoricalOptionsPrices,
    HistoricalQuotes,
    HistoricalStockPrices,
    OptionsContracts,
    PolygonPaginator,
    StockMetaData,
)
from data_pipeline.QuotePool import QuotePool
from db_tools.queries import lookup_multi_ticker_ids
from db_tools.utils import OptionTicker

from curator.proj_constants import POLYGON_BASE_URL, log
from curator.utils import pool_kwarg_config

planned_exceptions = (
    InvalidArgs,
    ProjClientConnectionError,
    ProjBaseException,
    ProjClientResponseError,
    ProjIndexError,
    ProjTimeoutError,
)


async def api_pool_downloader(
    paginator: PolygonPaginator,
    args_data: list = None,
    pool_kwargs: dict = {},
    batch_num: int = None,
):
    """This function creates a process pool to download data from the polygon api and store it in json files.
    It is the base module co-routine for all our data pulls.
    It generates the urls to be queried, creates and runs a process pool to perform the I/O queries.
    The results for each request are returned via PoolResults generator.

    Args:
        paginator: PolygonPaginator object, specific to the endpoint being queried,
        args_data: list of data args to be used to generate pool args
        pool_kwargs: kwargs to be passed to the process pool

    url_args: list of tuples, each tuple contains the args for the paginator's download_data method
    """
    log.info("generating urls to be queried")
    url_args = paginator.generate_request_args(args_data)
    if url_args:
        log.info("fetching data from polygon api")
        log.debug(f"tasks: {len(url_args)}")
        pool_kwargs = dict(**pool_kwargs, **{"init_client_session": True, "session_base_url": POLYGON_BASE_URL})
        pool_kwargs = pool_kwarg_config(pool_kwargs)
        async with Pool(**pool_kwargs) as pool:
            await pool.starmap(paginator.download_data, url_args)

        log.info(f"finished downloading data for {paginator.paginator_type}. Process pool closed")
    elif batch_num:
        log.info(f"no data to download for {paginator.paginator_type} batch: {batch_num}")


async def download_stock_metadata(tickers: list[str], all_: bool = True):
    """This function downloads stock metadata from polygon and stores it in /data/StockMetaData/*.json."""

    if all_:
        log.info("pulling ticker metadata for all tickers")
    else:
        log.info(f"pulling ticker metadata for tickers: {tickers}")
    meta = StockMetaData(tickers, all_)
    pool_kwargs = {"processes": 1, "childconcurrency": 1, "queuecount": 1}
    await api_pool_downloader(meta, pool_kwargs=pool_kwargs)


async def download_stock_prices(ticker_id_lookup: dict[str, int], start_date: str, end_date: str):
    tickers = list(ticker_id_lookup.keys())
    prices = HistoricalStockPrices(start_date, end_date)
    pool_kwargs = {"childconcurrency": 5, "processes": 1, "queuecount": 1}
    await api_pool_downloader(paginator=prices, args_data=tickers, pool_kwargs=pool_kwargs)


async def download_options_contracts(
    tickers: list[str] = None,
    ticker_id_lookup: dict | None = None,
    months_hist: int = 24,
):
    # NOTE: if refreshing, just pull the current month, months_hist = 1
    if not tickers and not ticker_id_lookup:
        raise InvalidArgs("Must provide either tickers or ticker_id_lookup")
    elif not tickers:
        tickers = list(ticker_id_lookup.keys())
    elif not ticker_id_lookup:
        tickers_w_ids = await lookup_multi_ticker_ids(tickers, stock=True)
        ticker_id_lookup = {x[0]: x[1] for x in tickers_w_ids}
    options = OptionsContracts(tickers, ticker_id_lookup, months_hist)
    await api_pool_downloader(paginator=options, args_data=tickers)


async def download_options_prices(o_tickers: list[tuple[str, int, datetime, str]], months_hist: int = 24):
    """This function downloads options prices from polygon and stores it as local json.

    Args:
        o_tickers: list of OptionTicker tuples
        month_hist: number of months of history to pull
    """
    pool_kwargs = {"childconcurrency": 250, "maxtasksperchild": 50000}
    op_prices = HistoricalOptionsPrices(months_hist=months_hist)
    await api_pool_downloader(paginator=op_prices, pool_kwargs=pool_kwargs, args_data=o_tickers)


async def download_options_snapshots(o_tickers: list[OptionTicker]):
    """This function downloads options snapshots from polygon and stores it as local json.

    Args:
        o_tickers: list of OptionTicker tuples
    """
    pool_kwargs = {"childconcurrency": 250, "maxtasksperchild": 50000}
    op_snapshots = CurrentContractSnapshot()
    await api_pool_downloader(paginator=op_snapshots, pool_kwargs=pool_kwargs, args_data=o_tickers)


async def download_options_quotes(ticker: str, o_tickers: list[OptionTicker], months_hist: int = 24):
    """This function downloads options quotes from polygon and stores it as local json.

    Args:
        o_tickers: list of OptionTicker tuples
        month_hist: number of months of history to pull
    """
    pool_kwargs = {"childconcurrency": 14, "maxtasksperchild": 1000, "processes": 30}
    o_ticker_lookup = {x.o_ticker: x.id for x in o_tickers}
    op_quotes = HistoricalQuotes(months_hist=months_hist, o_ticker_lookup=o_ticker_lookup)
    BATCH_SIZE_OTICKERS = 1000
    batch_o_tickers = [x for x in o_tickers if x.underlying_ticker == ticker]
    for i in range(0, len(batch_o_tickers), BATCH_SIZE_OTICKERS):
        small_batch = batch_o_tickers[i : i + BATCH_SIZE_OTICKERS]

        log.info(f"downloading quotes for {i+BATCH_SIZE_OTICKERS}/{len(batch_o_tickers)} batch of o_tickers")

        await api_quote_downloader(
            paginator=op_quotes,
            pool_kwargs=pool_kwargs,
            args_data=small_batch,
        )
    log.info(f"-- Completely done downloading {ticker}")


async def api_quote_downloader(
    paginator: HistoricalQuotes,
    args_data: list = None,
    pool_kwargs: dict = {},
):
    """This function creates a process pool to download data from the polygon api and store it in json files.
    It is the base module co-routine for all our data pulls.
    It generates the urls to be queried, creates and runs a process pool to perform the I/O queries.
    The results for each request are returned via PoolResults generator.

    Args:
        paginator: PolygonPaginator object, specific to the endpoint being queried,
        args_data: list of data args to be used to generate pool args
        pool_kwargs: kwargs to be passed to the process pool

    url_args: list of tuples, each tuple contains the args for the paginator's download_data method
    """
    log.info("generating urls to be queried")
    url_args, o_ticker_count_mapping = paginator.generate_request_args(args_data)
    if url_args:
        log.info("fetching data from polygon api")
        log.info(f"tasks: {len(url_args)}")
        pool_kwargs = dict(**pool_kwargs, **{"init_client_session": True, "session_base_url": POLYGON_BASE_URL})
        pool_kwargs = pool_kwarg_config(pool_kwargs)
        log.info("creating quote pool")
        async with QuotePool(**pool_kwargs, o_ticker_count_mapping=o_ticker_count_mapping) as pool:
            log.info("deploying QuoteWorkers in Pool")
            await pool.starmap(paginator.download_data, url_args)

        log.info(f"finished downloading data for {paginator.paginator_type}. Process pool closed")
