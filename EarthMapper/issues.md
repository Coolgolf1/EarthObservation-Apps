# Issues 

## SQL Query Geometry
One of the main issues has been trying to query the column `geometry` as this column has the following format:

```JSON
{
    "type": "<type>",
    "coordinates": [
        [
            [
                104...,
                50...,
            ],
            [
                104...,
                50...,
            ]
        ]
    ]
}
```

This is difficult to query using only SQL and not using python in order to filter the coordinates. But using python would reduce performance compared to applying the query on DataBricks's Lakebase which is prepared to handle large requests.

Once the query worked it took a long time for the DB to respond, so I had to optimise the query in order to speed it up for UX.

### Possible Solutions
1. Separate the data into columns and clean data (silver layer)
2. Index the DB's geometry columns
3. Optimise the SQL query (harder)

## SQL Query Inefficient
Related to the last issue, I should make the backend more efficient, and try to use for loops as least as possible, as they are really costly to process the query results.

## Frontend Being Slow
I should try to optimise the plotting process 