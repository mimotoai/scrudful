from datetime import datetime
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from scrudful.fastapi import scrudful
import uvicorn

current_datetime = datetime.now()
dummy_data = [
    {
        "id": 1,
        "name": "John",
        "age": 22,
        "created_at": datetime(2021, 5, 24),
        "updated_at": datetime(2021, 5, 25)
    },
    {
        "id": 2,
        "name": "Alice",
        "age": 24,
        "created_at": datetime(2021, 5, 24),
        "updated_at": datetime(2021, 5, 25)
    },
    {
        "id": 3,
        "name": "Bob",
        "age": 25,
        "created_at": datetime(2021, 5, 24),
        "updated_at": datetime(2021, 5, 25)
    }
]


class UserModel(BaseModel):
    id: int
    name: str
    age: int


app = FastAPI()


def get_etag(*args, **kwargs):
    return "USER_{}_".format(str(kwargs["id"]))


def get_last_modified_date(*args, **kwargs):
    modified_date = [d['updated_at'] for d in dummy_data if d["id"] == kwargs["id"]]

    return modified_date[0] if len(modified_date) else datetime.now()


def get_schema_link(*args, **kwargs):
    return "http://scrud.io/schemas/semantic_http.json"


def get_schema_rel(*args, **kwargs):
    return "http://www.w3.org/ns/json-ld#context"


def get_schema_type(*args, **kwargs):
    return "application/ld+json"


def get_context_link(*args, **kwargs):
    return "http://scrud.io/schemas/semantic_http.json"


def get_context_rel(*args, **kwargs):
    return "http://www.w3.org/ns/json-ld#context"


def get_context_type(*args, **kwargs):
    return "application/ld+json"


@app.get("/user/{id}")
@scrudful(
    get_etag,
    get_last_modified_date,
    get_schema_link,
    get_schema_rel,
    get_schema_type,
    get_context_link,
    get_context_rel,
    get_context_type,
)
async def get_request(request: Request, response: Response, id: int):
    response = {"status": False, "message": "Record not found"}
    data = [d for d in dummy_data if d["id"] == id]
    if data:
        response["status"] = True
        response["message"] = "Record Fetched"
        response["payload"] = data[0]
    return response


@app.post("/user")
@scrudful(
    get_schema_link,
    get_schema_rel,
    get_schema_type,
    get_context_link,
    get_context_rel,
    get_context_type,
)
async def post_request(request: Request, response: Response):
    new_user_record = await request.json()
    ids = [d['id'] for d in dummy_data]
    new_user_record["id"] = len(ids) + 1
    new_user_record["created_at"] = datetime(2021, 5, 24)
    new_user_record["updated_at"] = datetime(2021, 5, 24)

    dummy_data.append(new_user_record)

    resp = {
        "status": True,
        "message": "Record Created",
        "payload": new_user_record
    }

    return resp


@app.put("/user/{id}")
@scrudful(
    get_etag,
    get_last_modified_date,
    get_schema_link,
    get_schema_rel,
    get_schema_type,
    get_context_link,
    get_context_rel,
    get_context_type,
)
async def put_request(request: Request, response: Response, id: int):
    resp = {
        "status": False,
        "message": "Record Not Found"
    }
    update_user_record = await request.json()
    record = [d for d in dummy_data if d["id"] == id]

    if len(record):
        for key in update_user_record:
            record[0][key] = update_user_record[key]
        # Last modified date updated
        record[0]["updated_at"] = datetime.now()
        resp["status"] = True
        resp["message"] = "Record Updated"
        resp["payload"] = record[0]
    else:
        response.status_code = 404

    return resp


@app.delete("/user/{id}")
@scrudful(
    get_etag,
    get_last_modified_date
)
async def delete_request(request: Request, response: Response, id: int):
    resp = {
        "status": False,
        "message": "Record Not Found"
    }
    record = [d for d in dummy_data if d["id"] == id]

    if len(record):
        resp["status"] = True
        resp["message"] = "Record Deleted"
    else:
        response.status_code = 404

    return resp

if __name__ == "__main__":
    uvicorn.run(app, port=8000)
