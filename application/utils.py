import os
import math
import random
import sqlalchemy
from io import BytesIO
from hashlib import sha256

import pandas
import requests
from bs4 import BeautifulSoup, Comment

os.chdir(os.path.dirname(__file__))

BASE_URL = os.environ.get('FRIEDRICH_PORTAL_URL')
USERNAME = os.environ.get('FRIEDRICH_PORTAL_USERNAME')
PASSWORD = os.environ.get('FRIEDRICH_PORTAL_PASS')
DATABASE = os.environ.get('DATABASE_URL')

engine = sqlalchemy.create_engine(DATABASE)


def fetch_approved_quotes_from_website() -> bytes:
    """
    uses requests module to log in to the Friedrich sales rep
    portal and download the list of approved job quotes

    Returns excel file as bytes
    """

    def generate_cache_num():
        return math.floor(random.random()*100000)

    with requests.Session() as session:

        # set headers to avoid getting flagged as a robot
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:95.0) Gecko/20100101 Firefox/95.0"}
        )

        # log in
        login_request = "{base}/Ajax_ValidateSignOn.aspx?EmailAddress={user}&Password={password}&Cache={cache}"
        login_request = login_request.format(base=BASE_URL, user=USERNAME, password=PASSWORD, cache=generate_cache_num())
        response = session.get(login_request)

        # navigate to dashboard
        response = session.get(f"{BASE_URL}/Dashboard2.aspx")

        # load quotes
        soup = BeautifulSoup(response.text, "html.parser")
        contact_id = soup.find(string=lambda text: isinstance(text, Comment)).extract().replace("ContactID: ","")
        quotes_request = f"{BASE_URL}/Ajax_DashboardV2_LoadQuotes.aspx?ContactID={contact_id}&Cache={generate_cache_num()}"
        response = session.get(quotes_request)

        # request file
        soup = BeautifulSoup(response.text, "html.parser")
        onclick_url = soup.find('div').find('div')['onclick'] 
        begin = onclick_url.index("(")+2
        end = onclick_url.index(',')-1
        sub_url = onclick_url[begin:end]
        file_request = f"{BASE_URL}/{sub_url}"
        file_response = session.get(file_request)
        file_content = file_response.content

    return file_content


def convert_to_df(file: bytes) -> pandas.DataFrame:
    """
    converts the file in bytes format to a pandas dataframe
    using an in-memory file

    Returns a pandas DataFrame
    """

    with BytesIO(file) as data:
        return pandas.read_excel(data, skiprows=2)


def get_data() -> pandas.DataFrame:
    """
    wrapper function to make data retrieval interface a simple function call
    """
    data_bytes = fetch_approved_quotes_from_website()
    data_table = convert_to_df(data_bytes)
    return data_table


def append_hashid_col(data: pandas.DataFrame) -> pandas.DataFrame:
    """
    appends a new column with the hash ID for each row

    hash ID is generated by concatenating all row values into one string
    and applying sha256 from hashlib to the string

    returns a new dataframe
    """
    new_df = data.copy()
    cols_to_hash = new_df.columns.tolist()

    def func(row, cols):
        col_data = []
        for col in cols:
            col_data.append(str(row.at[col]))

        col_combined = ''.join(col_data).encode()
        hashed_col = sha256(col_combined).hexdigest()
        return hashed_col

    new_df["hashid"] = new_df.apply(lambda row: func(row, cols_to_hash), axis=1)
    return new_df


def compare_tables(new_table: pandas.DataFrame) -> pandas.DataFrame:

    new_table_cp = new_table.copy()
    output_columns = new_table_cp.columns.tolist()
    output_columns.remove("hashid")

    old_table = get_saved_data()

    new_table_cp.set_index('hashid', inplace=True)
    old_table.set_index('hashid', inplace=True)

    left_join = new_table_cp.join(
        old_table,
        how="left",
        lsuffix="_left",
        rsuffix="_right")

    right_join = new_table_cp.join(
        old_table,
        how="right",
        lsuffix="_left",
        rsuffix="_right")

    left_not_right = left_join[left_join['Project Name_right'].isnull()]
    right_not_left = right_join[right_join['Project Name_left'].isnull()]

    #fix column names for output tables
    left_join_columns = left_join.columns.tolist()
    left_join_columns_fixed = [col.replace('_left','') for col in left_join_columns]
    right_join_columns = right_join.columns.tolist()
    right_join_columns_fixed = [col.replace('_right','') for col in right_join_columns]

    # Re-set all colums in all output tables
    left_not_right.set_axis(left_join_columns_fixed, axis=1, inplace=True)
    right_not_left.set_axis(right_join_columns_fixed, axis=1, inplace=True)

    # return dict of results
    differences = {}
    added = left_not_right.loc[:,output_columns]
    removed = right_not_left.loc[:,output_columns]

    for key, dataset in [("Added",added), ("Removed",removed)]:
        dataset: pandas.DataFrame
        if not dataset.empty:
            differences[key] = dataset

    return differences


def save_to_database(data: pandas.DataFrame):

    with engine.connect() as conn:
        data.to_sql('data', conn, if_exists='replace', index=False)
    return


def get_saved_data() -> pandas.DataFrame:

    with engine.connect() as conn:
        result = pandas.read_sql('SELECT * FROM data', conn)
    return result


def run_quote_check():

    new_table = get_data()
    new_table = append_hashid_col(new_table)

    if get_saved_data().empty:
        save_to_database(new_table)
        return {}
    else:
        diffs = compare_tables(new_table)
        save_to_database(new_table)
        return diffs


def format_to_html_summary(df: pandas.DataFrame) -> str:
    """
    Takes pandas dataframe and returns 
    a custom summary format in an HTML string
    """
    project_col_names = ['Rep Name','Project Name','Project City',
            'Project State','Quote Name','Create Date', 'Quote Status']

    all_projects = df.loc[:,project_col_names].drop_duplicates().to_dict(orient='index')
    
    all_projects_dict = [tuple(value.values()) for value in all_projects.values()]

    result = ""

    for project in all_projects_dict:
        customer, proj_name, city, state, quote, date, status = project
        project_html = f"<hr><h3>Customer: <span style=\"font-weight: lighter\">{customer}</span>\n \
                <h3>Project Location: <span style=\"font-weight: lighter\">{city}, {state}</span>\n \
                <h3>Name: <span style=\"font-weight: lighter\">{proj_name}</span>\n \
                <h3>Quote Name: <span style=\"font-weight: lighter\">{quote}</span>\n \
                <h3>Date Created: <span style=\"font-weight: lighter\">{date}</span> \
                <h3>Status: <span style=\"font-weight: lighter\">{status}</span>"
        records = df.loc[
            (df['Rep Name'] == customer)
            & (df['Project Name'] == proj_name)
            & (df['Project City'] == city)
            & (df['Project State'] == state)
            & (df['Quote Name'] == quote), 
            ['Product Group', 'Product SKU', 'Product Quantity', 'Product Total Amount']]
        
        records_html_table = records.to_html(index=False)

        result += project_html + records_html_table + "<br>"
    
    return result
