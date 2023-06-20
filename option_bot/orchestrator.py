import asyncio
from argparse import Namespace
from datetime import datetime
from multiprocessing import cpu_count
from typing import Awaitable

import uvloop
from aiomultiprocess import Pool  # , set_start_method
from sentry_sdk import capture_exception

from option_bot.db_tools.queries import (
    delete_stock_ticker,
    lookup_multi_ticker_ids,
    lookup_ticker_id,
    query_all_stock_tickers,
    query_options_tickers,
    ticker_imported,
    update_options_prices,
    update_options_tickers,
    update_stock_metadata,
    update_stock_prices,
)

# from option_bot.db_tools.uploader import Uploader
from option_bot.exceptions import (
    InvalidArgs,
    ProjBaseException,
    ProjClientConnectionError,
    ProjClientResponseError,
    ProjIndexError,
    ProjTimeoutError,
)
from option_bot.polygon_utils import (
    HistoricalOptionsPrices,
    HistoricalStockPrices,
    OptionsContracts,
    PolygonPaginator,
    StockMetaData,
)
from option_bot.proj_constants import log, MAX_CONCURRENT_REQUESTS, POLYGON_BASE_URL


# set_start_method("fork")

CPUS = cpu_count() - 2


planned_exceptions = (
    InvalidArgs,
    ProjClientConnectionError,
    ProjBaseException,
    ProjClientResponseError,
    ProjIndexError,
    ProjTimeoutError,
)

pool_default_kwargs = {
    "processes": CPUS,
    "exception_handler": capture_exception,
    "loop_initializer": uvloop.new_event_loop,
    "childconcurrency": int(MAX_CONCURRENT_REQUESTS / CPUS),
    "queuecount": int(CPUS / 3),
    "init_client_session": True,
    "session_base_url": POLYGON_BASE_URL,
}


def pool_kwarg_config(kwargs: dict) -> dict:
    """This function updates the kwargs for an aiomultiprocess.Pool from the defaults."""
    pool_kwargs = pool_default_kwargs.copy()
    pool_kwargs.update(kwargs)
    return pool_kwargs


async def etl_pool_uploader(
    paginator: PolygonPaginator, upload_func: Awaitable, record_size: int, expected_args: int, pool_kwargs: dict = {}
):
    """This function will create a process pool to concurrently upload the downloaded json to the db

    Args:
        upload_func: function to upload the data to the db, matching the endpoint/data type being queried
        record_size: number of fields per record, used to estimate the batch size
        expected_args: number of arguments expected to be passed to the pool. Based on files in directory

    """
    pass
    # uploader = Uploader(upload_func, expected_args, record_size)
    # async with Pool(**pool_kwargs) as pool:
    # if result is not None:
    #     log.info(f"Processing {paginator.paginator_type} results for arg {url_args[result_ix]}")
    #     clean_data = paginator.clean_data(result)
    #     await uploader.process_clean_data(clean_data)
    # else:
    #     uploader.update_expected_records()
    #     log.warning(
    #         f"no {paginator.paginator_type} results for arg {url_args[result_ix]}"
    #        )  # depends on results returned in order


async def api_pool_downloader(
    paginator: PolygonPaginator,
    pool_kwargs: dict = {},
):
    """This function creates a process pool to download data from the polygon api and store it in json files.
    It is the base module co-routine for all our data pulls.
    It generates the urls to be queried, creates and runs a process pool to perform the I/O queries.
    The results for each request are returned via PoolResults generator.

    Args:
        paginator: PolygonPaginator object, specific to the endpoint being queried


    """
    log.info("generating urls to be queried")
    url_args = paginator.generate_request_args()

    log.info("fetching data from polygon api")
    pool_kwargs = pool_kwarg_config(pool_kwargs)
    async with Pool(**pool_kwargs) as pool:
        await pool.starmap(paginator.download_data, url_args)

    log.info(f"finished downloading data for {paginator.paginator_type}. Process pool closed")


async def add_tickers_to_universe(kwargs_list):
    tickers = [x["ticker"] for x in kwargs_list]

    async with Pool(
        processes=max(len(tickers), CPUS), exception_handler=capture_exception, loop_initializer=uvloop.new_event_loop
    ) as pool:
        await pool.map(fetch_stock_metadata, tickers)

    ticker_ids = await lookup_multi_ticker_ids(tickers)
    if len(ticker_ids) != len(kwargs_list):
        raise InvalidArgs(
            f"uneven number of ticker inputs and retrieved \
ticker_ids, ticker_args: {len(kwargs_list)}, ticker_ids: {len(ticker_ids)}"
        )
    args_list = [
        [
            kwargs_list[i]["ticker"],
            ticker_ids[i],
            kwargs_list[i]["start_date"],
            kwargs_list[i]["end_date"],
            kwargs_list[i]["months_hist"],
        ]
        for i in range(len(kwargs_list))
    ]

    async with Pool(
        processes=CPUS, exception_handler=capture_exception, loop_initializer=uvloop.new_event_loop
    ) as pool:
        await pool.starmap(import_tickers_and_contracts_process, args_list)

    log.info("queuing options contracts metadata")
    op_args = await prep_options_prices_args(tickers=[x[0] for x in args_list])

    log.info("fetching options contracts prices")
    async with Pool(
        processes=CPUS,
        exception_handler=capture_exception,
        loop_initializer=uvloop.new_event_loop,
        maxtasksperchild=10,
        queuecount=CPUS,  # childconcurrency=20
    ) as pool:
        await pool.starmap(fetch_options_prices, op_args)


async def import_all_tickers(args: Namespace):
    log.info("fetching all stock ticker metadata")
    ticker_lookup = await import_all_ticker_metadata()
    args_list = [
        (
            list(x.keys())[0],  # "ticker":
            list(x.values())[0],  # "ticker_id":
            args.startdate,  # "start_date":
            args.enddate,  # "end_date":
            args.monthhist,  # "months_hist":
        )
        for x in ticker_lookup
    ]

    log.info("importing all stock prices and options contract metadata")
    async with Pool(
        processes=CPUS,
        exception_handler=capture_exception,
        loop_initializer=uvloop.new_event_loop,
        maxtasksperchild=64,
        childconcurrency=3,
        queuecount=CPUS,
    ) as pool:
        await pool.starmap(import_tickers_and_contracts_process, args_list)

    log.info("queuing options contracts metadata")
    op_args = await prep_options_prices_args(tickers=["all_"], all_=True)

    log.info("fetching options contracts prices")
    async with Pool(
        processes=CPUS,
        loop_initializer=uvloop.new_event_loop,
        # maxtasksperchild=100,
        childconcurrency=int(100 / CPUS),
        queuecount=int(CPUS / 3),
        init_client_session=True,
        session_base_url="https://api.polygon.io",
    ) as pool:
        async for result in pool.starmap(fetch_options_prices, op_args):
            pass


async def import_tickers_and_contracts_process(
    ticker: str,
    ticker_id: int,
    start_date: datetime,
    end_date: datetime,
    months_hist: int,
):
    """
    This is the event loop to import prices and options contracts for a ticker.
    Both all_ and individual import processes will hit this loop.
    AioMultiprocessing will apply multiple processes to this event loop to scale it

    The input dict will have ticker, ticker_id and other args to initiate stock_prices and options imports.

    This function needs to be run after stock ticker metadata has already been retrieved.

    """

    await asyncio.gather(
        fetch_stock_prices(ticker=ticker, ticker_id=ticker_id, start_date=start_date, end_date=end_date),
        fetch_options_contracts(ticker=ticker, ticker_id=ticker_id, months_hist=months_hist),
    )


async def prep_options_prices_args(tickers: list[str], all_=False):
    """Function to prepare the list of list_args to be used as input to the fetch_options_prices function
    Inputs:
        ticker_args: list of tickers:str
    """
    if all_:
        o_tickers = await query_options_tickers(stock_tickers=["all_"], all_=True)
    else:
        o_tickers = await query_options_tickers(stock_tickers=tickers)

    return o_tickers


async def remove_tickers_from_universe(tickers: list[str]):
    for ticker in tickers:
        log.info(f"deleting ticker {ticker}")
        await delete_stock_ticker(ticker)
        log.info(f"ticker {ticker} successfully deleted")


async def import_all_ticker_metadata():
    """
    This function fetches all stock ticker metadata from polygon.
    It then loads to postgres, and returns a list of rows with each ticker's ticker_id

    the ticker_lookup produced will be a list of dicts
    each dict will be a {"ticker": "ticker_id"} pair.

    eg: [{'AAWW': 125}, {'ABGI': 138}, {'AA': 94}]"""

    log.info("fetching all stock ticker metadata")
    await fetch_stock_metadata(tickers=["all_"], all_=True)
    ticker_results = await query_all_stock_tickers()
    ticker_lookup = [{x[1]: x[0]} for x in ticker_results]
    return ticker_lookup


async def fetch_stock_metadata(tickers: list[str], all_: bool = True):
    if all_:
        log.info("pulling ticker metadata for all tickers")
    else:
        log.info(f"pulling ticker metadata for tickers: {tickers}")
    meta = StockMetaData(tickers, all_)
    pool_kwargs = {"processes": 1, "childconcurrency": 1, "queuecount": 1}
    await api_pool_downloader(meta, pool_kwargs=pool_kwargs)


async def fetch_stock_prices(ticker: str, start_date: str, end_date: str, ticker_id: int | None = None):
    if not ticker_id:
        ticker_id = await lookup_ticker_id(ticker, stock=True)
    # TODO: Add exception handling here with a sleep function incase metadata has yet to populate
    log.info(f"pulling ticker prices for ticker: {ticker}")
    prices = HistoricalStockPrices(ticker, ticker_id, start_date, end_date)
    await prices.fetch()
    for batch in prices.clean_data_generator:
        await update_stock_prices(batch)
    await ticker_imported(ticker_id)
    log.info(f"{ticker} successfully imported")


async def test_query():
    results = (await query_options_tickers(["SPY"]),)
    return results  # 9912 is SPY id


async def fetch_options_contracts(
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
    await api_pool_downloader(options)


async def fetch_options_prices(o_ticker: str, o_ticker_id: int, expiration_date: datetime, month_hist: int = 24):
    # log.info(f"pulling options contract pricing for ticker: {o_ticker}")

    try:
        o_prices = HistoricalOptionsPrices(o_ticker, o_ticker_id, expiration_date, month_hist)
        await o_prices.fetch()
        #    log.info(f"uploading option prices for ticker: {o_ticker}")
        for batch in o_prices.clean_data_generator:
            await update_options_prices(batch)

    except planned_exceptions:  # as e:
        pass
    #    log.warning(e, exc_info=False)
    #   log.warning(f"failed to fetch options prices for {o_ticker}, o_ticker_id: {o_ticker_id}")

    except Exception as e:
        log.error(e, exc_info=True)

    finally:
        pass
        # log.info(f"finished uploading option prices for ticker: {o_ticker}, o_ticker_id: {o_ticker_id}")


async def main():
    ticker_lookup = await import_all_ticker_metadata()
    ticker_lookup = {list(x.keys())[0]: list(x.values())[0] for x in ticker_lookup}
    await fetch_options_contracts(ticker_id_lookup=ticker_lookup)


if __name__ == "__main__":
    asyncio.run(main())
    # fetch_options_contracts(["SPY", "HOOD", "IVV"]))
