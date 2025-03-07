import re
import sys
import time

import numpy as np
import matplotlib.pyplot as plt
import json

from psycopg2 import sql
from scipy.optimize import curve_fit
import os

project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.append(project_root)
print(project_root)
# Row-level security imports
from controller.baseline.pg_row_security.row_level_security import (
    disable_row_level_security, drop_database_users, create_database_users, enable_row_level_security,
    get_db_connection_for_many_users
)
from services.config import get_db_connection
from basic_benchmark.common_function import save_query_plan, ground_truth_func
from controller.clear_database import clear_tables

topk = None
sel = None


# Step 1: Predicted recall calculation
def calculate_x(ef_search, block_selectivity, topk):
    """
    Calculate the effective x value based on ef_search, block selectivity, and topk.
    Formula: x = ef_search * block_selectivity / topk
    """
    return ef_search * block_selectivity / topk


def search_documents_rls_for_analysis_with_execution_time(user_id, query_vector, topk, ef_search_values):
    """
    Search documents with role-level security for multiple ef_search values using EXPLAIN ANALYZE.
    Computes SQL execution time for each ef_search value.

    :param user_id: User ID for which to execute the query.
    :param query_vector: Query vector for similarity search.
    :param topk: Number of top results to retrieve.
    :param ef_search_values: List of ef_search values to evaluate.
    :return: A dictionary mapping each ef_search to its (query time, total rows).
    """
    results = {}
    conn = get_db_connection_for_many_users(user_id)  # Reuse the same connection for this user
    cur = conn.cursor()

    try:
        # Disable parallelism for consistent timing
        cur.execute(f"SET max_parallel_workers_per_gather = 0;")
        # Query for the role's document blocks
        table_name = sql.Identifier("documentblocks")

        # Count total rows in the table
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {};").format(table_name))
        n_total_rows = cur.fetchone()[0]  # Total rows for the first role

        # Prepare regex for join time parsing
        subplan_pattern = re.compile(r"^SubPlan 2$")
        actual_time_pattern = re.compile(r"actual time=\d+\.\d+\.\.(\d+\.\d+)")

        # Process each ef_search value
        for ef_search in ef_search_values:
            cur.execute(f"SET LOCAL hnsw.ef_search = {ef_search};")  # Dynamically set ef_search

            # Execute EXPLAIN ANALYZE to time the query
            explain_query = sql.SQL(
                """
                EXPLAIN (ANALYZE, VERBOSE)
                SELECT block_id, document_id, block_content, vector <-> %s::vector AS distance
                FROM {}
                ORDER BY distance
                LIMIT %s
                """
            ).format(table_name)

            cur.execute(explain_query, [query_vector, topk])
            explain_plan = cur.fetchall()

            # Parse query time and join time from EXPLAIN ANALYZE
            total_adjusted_time = 0  # Initialize cumulative adjusted query time
            join_times = []
            in_subplan = False  # Track whether we're inside SubPlan 2

            for row in explain_plan:
                line = row[0].strip()

                # Detect entry into SubPlan 2
                if subplan_pattern.match(line):
                    in_subplan = True
                    continue  # Proceed to the next line

                # If inside SubPlan 2, extract actual time
                if in_subplan:
                    actual_time_match = actual_time_pattern.search(line)
                    if actual_time_match:
                        join_times.append(float(actual_time_match.group(1)) * 1000 * 1000)  # Convert ms to ns
                        in_subplan = False  # Exit SubPlan 2 after recording the first actual time

                # Parse overall execution time
                if "Execution Time" in line:
                    query_time = float(line.split()[-2]) * 1000 * 1000  # Convert ms to ns
                    if join_times:
                        total_adjusted_time += query_time - join_times[-1]  # Subtract the latest join time

            # Store results for the current ef_search
            results[ef_search] = (total_adjusted_time, n_total_rows)

    finally:
        cur.close()
        conn.close()

    return results


# Step 2: Actual recall calculation
def search_documents_rls_for_analysis(user_id, query_vector, topk, ef_search_values):
    """
    Search documents with row-level security using a specified ef_search value.
    This function retrieves the topk results based on vector similarity.
    """
    results = []
    conn = get_db_connection_for_many_users(user_id)  # Reuse the same connection for this user
    try:
        cur = conn.cursor()
        for ef_search in ef_search_values:
            cur.execute(f"SET LOCAL hnsw.ef_search = {ef_search};")
            query = """
                SELECT block_id, document_id, block_content, vector <-> %s::vector AS distance
                FROM DocumentBlocks
                ORDER BY distance
                LIMIT %s
            """
            cur.execute(query, [query_vector, topk])
            results.append(cur.fetchall())
    finally:
        cur.close()
        conn.close()
    return results


def search_documents_rls_for_join_time_analysis(user_id, query_vector, topk=5):
    """
    Search documents with row-level security using a specified ef_search value.
    This function retrieves the topk results based on vector similarity and analyzes
    execution times, particularly for joins under SubPlan 2.
    """
    import re
    import statistics

    conn = get_db_connection_for_many_users(user_id)
    cur = conn.cursor()

    # Collect join times under SubPlan 2
    join_times = []

    # Use EXPLAIN ANALYZE to capture query plan and execution statistics
    explain_query = """
        EXPLAIN (ANALYZE, VERBOSE)
        SELECT block_id, document_id, block_content, vector <-> %s::vector AS distance
        FROM DocumentBlocks
        ORDER BY distance
        LIMIT %s
    """
    cur.execute(explain_query, [query_vector, topk])
    explain_plan = cur.fetchall()

    # Regex to capture SubPlan 2 and actual time
    subplan_pattern = re.compile(r"^SubPlan 2$")
    actual_time_pattern = re.compile(r"actual time=\d+\.\d+\.\.(\d+\.\d+)")

    in_subplan = False  # Track whether we're inside SubPlan 2

    # Parse EXPLAIN ANALYZE output
    for row in explain_plan:
        line = row[0].strip()

        # Detect entry into SubPlan 2
        if subplan_pattern.match(line):
            in_subplan = True
            continue  # Proceed to the next line

        # If inside SubPlan 2, extract actual time
        if in_subplan:
            actual_time_match = actual_time_pattern.search(line)
            if actual_time_match:
                join_times.append(float(actual_time_match.group(1)) * 1000 * 1000)
                in_subplan = False  # Exit SubPlan 2 after recording the first actual time

    # Calculate the median join time from SubPlan 2
    median_join_time = statistics.median(join_times) if join_times else 0

    cur.close()
    conn.close()

    # Return query results and median join time
    return median_join_time


def calculate_actual_recall_batch(user_id, query_vector, topk, ground_truth_func, ef_search_values):
    """
    Calculate actual recalls for multiple ef_search values by comparing retrieved results with ground truth.

    :param user_id: The ID of the user performing the query.
    :param query_vector: Query vector for similarity search.
    :param topk: Number of top results needed.
    :param ground_truth_func: Function to retrieve ground truth results for recall calculation.
    :param ef_search_values: List of ef_search values for which recall needs to be calculated.
    :return: List of recall values corresponding to each ef_search.
    """
    # Perform batch search for all ef_search values
    search_results_batch = search_documents_rls_for_analysis(user_id, query_vector, topk, ef_search_values)

    # Get ground truth results
    ground_truth_results = ground_truth_func(user_id=user_id, query_vector=query_vector, topk=topk)

    # Convert ground truth results into a set of (document_id, block_id)
    ground_truth_combinations = set((result[1], result[0]) for result in ground_truth_results)

    # Calculate recall for each ef_search
    recall_values = []
    for search_results in search_results_batch:
        # Convert retrieved results into a set of (document_id, block_id)
        retrieved_combinations = set((result[1], result[0]) for result in search_results)

        # Calculate recall as the ratio of correct matches to ground truth size
        correct_matches = len(retrieved_combinations & ground_truth_combinations)
        recall = correct_matches / len(ground_truth_combinations) if ground_truth_combinations else 0
        recall_values.append(recall)

    return recall_values


def piecewise_recall_model(x, k, beta):
    """
    Piecewise model combining a linear function and a shifted sigmoid function:
    - Linear for x <= k * topk
    - Sigmoid for x > k * topk
    """
    global sel, topk

    # Calculate x_c as proportional to topk
    x_c = k * topk / sel

    # Sigmoid growth rate
    b = beta * 4 * sel / topk

    # Shift for smooth transition
    shift = x_c * sel / topk - 0.5

    # Piecewise function
    return np.piecewise(
        x,
        [x <= x_c, x > x_c],
        [
            lambda x: x * sel / topk,  # Linear part
            lambda x: (1 / (1 + np.exp(-b * (x - x_c)))) + shift  # Sigmoid part
        ]
    )


# Adjusted piecewise recall model with better initialization

def fit_piecewise_model(x, y):
    """
    Fit the piecewise model with k and beta as the variables.
    """
    global sel, topk

    # Initial guesses for k and beta
    k_initial = 0.4
    beta_initial = 1  # Default Sigmoid steepness

    # Fit k and beta
    params, _ = curve_fit(
        piecewise_recall_model,
        x,
        y,
        p0=[k_initial, beta_initial],
        maxfev=30000
    )
    return params


# Improved plot function with better logging and debugging
def plot_average_recall_with_piecewise_fit(query_dataset, ef_search_values, ground_truth_func):
    global topk, sel
    topk = query_dataset[0]["topk"]
    sel = np.mean([query["query_block_selectivity"] for query in query_dataset])  # Average selectivity

    start = time.time()
    recalls = {ef_search: [] for ef_search in ef_search_values}  # Initialize dictionary to store recalls

    for query in query_dataset:
        user_id = query["user_id"]
        query_vector = query["query_vector"]
        topk = query["topk"]

        recalls_actual = calculate_actual_recall_batch(user_id, query_vector, topk, ground_truth_func, ef_search_values)
        # Append recalls to the corresponding ef_search key
        for ef_search, recall in zip(ef_search_values, recalls_actual):
            recalls[ef_search].append(recall)

    average_recalls = [np.mean(recalls[ef_search]) for ef_search in ef_search_values]

    print(f"spend {time.time() - start} on search")
    # Fit piecewise function
    piecewise_params = fit_piecewise_model(ef_search_values, average_recalls)
    x_fit = np.linspace(min(ef_search_values), max(ef_search_values), 100)
    piecewise_fit = piecewise_recall_model(x_fit, *piecewise_params)

    # Debugging: Print intermediate values
    print(f"Fitted Parameters: {piecewise_params}")

    # Plot comparison
    plt.figure(figsize=(12, 6))
    plt.scatter(ef_search_values, average_recalls, color='blue', label="Average Recalls")
    plt.plot(x_fit, piecewise_fit, color="red", label=f"Piecewise Fit: Linear + Sigmoid")
    plt.axvline(piecewise_params[0] * topk / sel, color="green", linestyle="--",
                label=f"Split Point (x_c) = {piecewise_params[0] * topk / sel:.4f}")
    plt.xlabel("Ef_Search")
    plt.ylabel("Average Recall")
    plt.title("Average Recall vs Ef_Search with Improved Piecewise Fitting")
    plt.legend()
    plt.grid(True)
    plot_filename = f'recall_analysis.png'
    plt.savefig(plot_filename)
    plt.show()

    return piecewise_params


def get_hnsw_recall_parameters():
    disable_row_level_security()
    drop_database_users()
    create_database_users()
    enable_row_level_security()

    # Load query dataset
    benchmark_folder = os.path.join(project_root, "basic_benchmark")
    query_dataset_path = os.path.join(benchmark_folder, "query_dataset.json")
    with open(query_dataset_path, "r") as f:
        query_dataset = json.load(f)

    ef_search_values = [1, 3, 5, 7, 10, 20, 30, 40, 50, 75, 100, 150, 200, 300, 400, 800, 1000]

    # Perform analysis
    params = plot_average_recall_with_piecewise_fit(query_dataset, ef_search_values, ground_truth_func)
    return params


# Example usage
if __name__ == "__main__":
    start_time = time.time()
    get_hnsw_recall_parameters()
    print(f"total time:{time.time() - start_time}")
