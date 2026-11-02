"""Application: the use cases. They depend only on the domain and the ports, never on adapters.

    sync.py   keep each book's bets in sync: fetch, translate, run the conflict rule, store
    read.py   answer reads with canonical state and its freshness
"""
