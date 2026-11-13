'''
Test beancount methods
'''

import os

import pytest

import main

# Get the directory of the test files
TEST_DIR = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture(autouse=True)
def set_bean_file():
    """Set BEANCOUNT_FILE to use the test book.bean file and initialize connection."""
    # Set the environment variable to the test book.bean
    test_bean_file = os.path.join(TEST_DIR, "book.bean")
    os.environ["BEANCOUNT_FILE"] = test_bean_file
    # Reload the main module's BEAN_FILE
    main.BEAN_FILE = test_bean_file
    # Initialize the connection
    main.app.state.connection = main.preload_beancount_data()
    yield
    # Cleanup (optional)


@pytest.mark.asyncio
async def test_beancount_method():
    '''Invoke the beancount method directly'''
    actual = await main.beancount('SELECT 1')

    assert actual is not None
