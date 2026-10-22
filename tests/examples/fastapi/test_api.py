from fastapi.testclient import TestClient
from freezegun import freeze_time

from examples.fastapi.api import app

test_client = TestClient(app)


class TestGetRequestForExistingRecord():
    def test_get_req_200_response_data(self):
        # GET Request to fetch the user record
        # Testing success data response
        # Record was modified since the time requested in the headers
        response = test_client.get("/user/1", headers={
            "If-Match": '"USER_1_0.1.0"',
            "If-Modified-Since": "Tue, 24 May 2021 22:00:53 GMT"
        })
        resp_data = response.json()['payload']

        assert response.status_code == 200
        assert resp_data['id'] == 1
        assert resp_data['name'] == "John"
        assert resp_data['age'] == 22

    def test_get_req_200_response_headers(self):
        # GET Request to fetch the user record
        # Testing success header response
        # Record was modified since the time requested in the headers
        response = test_client.get("/user/1", headers={
            "If-Match": '"USER_1_0.1.0"',
            "If-Modified-Since": "Tue, 24 May 2021 22:00:53 GMT"
        })
        headers = response.headers

        assert response.status_code == 200
        assert headers['etag'] == '"USER_1_0.1.0"'
        assert headers['access-control-expose-headers'] == "Link"

    def test_get_req_304_response(self):
        # GET Request to fetch the user record
        # Testing the not modified status response
        # Record was not modified since the time requested in the headers
        response = test_client.get("/user/1", headers={
            "If-Match": '"USER_1_0.1.0"',
            "If-Modified-Since": "Tue, 25 May 2021 22:00:53 GMT"
        })

        assert response.status_code == 304

    def test_get_req_412_response(self):
        # GET Request to fetch the existing user record
        # Testing Precondition Failed response
        # Validation of mismatch in e-tag value
        response = test_client.get("/user/1", headers={
            "If-Match": '"USER_2_0.1.0"',
            "If-Modified-Since": "Tue, 25 May 2021 22:00:53 GMT"
        })

        assert response.status_code == 412


class TestPostRequestForCreatingRecord():
    def test_post_req_200_response_data(self):
        # POST Request to create a new user record
        # Testing success data response
        data = {
            "name": "Lisa",
            "age": 23
        }
        response = test_client.post("/user", json=data)
        resp_data = response.json()['payload']

        assert response.status_code == 200
        assert resp_data['id'] == 4
        assert resp_data['name'] == "Lisa"
        assert resp_data['age'] == 23

    def test_get_req_200_response_data(self):
        # GET Request to fetch the new user record created by POST method
        # Testing the response data of the latest created record
        response = test_client.get("/user/4", headers={})
        resp_data = response.json()['payload']

        assert response.status_code == 200
        assert resp_data['id'] == 4
        assert resp_data['name'] == "Lisa"

    def test_get_req_200_response_headers(self):
        # GET Request to fetch the new user record created by POST method
        # Testing the response headers of the latest created record
        response = test_client.get("/user/4", headers={})
        headers = response.headers

        assert response.status_code == 200
        assert headers['etag'] == '"USER_4_0.1.0"'
        assert headers['access-control-expose-headers'] == "Link"


class TestPutRequest():
    def test_put_req_400_response(self):
        # PUT Request to update the user record
        # Testing the 400 (Bad Request) response | Due to missing required headers
        data = {"age": 34}
        response = test_client.put(
            "/user/4",
            json=data,
            headers={}
        )
        resp_data = response.json()

        assert response.status_code == 400
        assert resp_data["missing-required-headers"] == ["If-Match", "If-Unmodified-Since"]  # noqa

    def test_put_req_412_response(self):
        # PUT Request to update the user record
        # Testing the 412 (Precondition Failed) response
        # Validation of mismatch in e-tag & last-modified value
        data = {"age": 34}
        response = test_client.put(
            "/user/4",
            json=data,
            headers={
                "If-Match": '"USER_2_0.1.0"',
                "If-Unmodified-Since": "Tue, 24 May 2021 00:00:00 GMT"
            }
        )

        assert response.status_code == 412

    @freeze_time("2021-05-25")
    def test_put_req_200_response_data(self):
        # PUT Request to update the user record
        # Testing the success 200 response data
        data = {"age": 34}
        response = test_client.put(
            "/user/4",
            json=data,
            headers={
                "If-Match": '"USER_4_0.1.0"',
                "If-Unmodified-Since": "Tue, 24 May 2021 00:00:00 GMT"
            }
        )
        resp_data = response.json()['payload']

        assert response.status_code == 200
        assert resp_data['id'] == 4
        assert resp_data['age'] == 34

    def test_get_req_200_response_headers(self):
        # GET Request to check the user record updated by PUT method
        # Testing the last-modified headers of the latest updated data
        response = test_client.get("/user/4", headers={
            "If-Match": '"USER_4_0.1.0"',
            "If-Modified-Since": "Tue, 24 May 2021 00:00:00 GMT"
        })
        headers = response.headers

        assert response.status_code == 200
        assert headers['last-modified'] == "Tue, 25 May 2021 00:00:00 GMT"

    def test_get_req_304_response(self):
        # GET Request to check the user record updated by PUT method
        # Testing if the records has some update since it was last updated
        response = test_client.get("/user/4", headers={
            "If-Match": '"USER_4_0.1.0"',
            "If-Modified-Since": "Tue, 25 May 2021 22:00:53 GMT"
        })

        assert response.status_code == 304


class TestDeleteRequest():
    def test_delete_req_400_response(self):
        # DELETE Request to remove the user record
        # Testing the 400 (Bad Request) response | Due to missing required headers
        response = test_client.delete(
            "/user/4",
            headers={}
        )
        resp_data = response.json()

        assert response.status_code == 400
        assert resp_data["missing-required-headers"] == ["If-Match", "If-Unmodified-Since"]  # noqa

    def test_delete_req_412_response(self):
        # DELETE Request to update the user record
        # Testing the 412 (Precondition Failed) response
        # Validation of mismatch in e-tag & last-modified value
        response = test_client.delete(
            "/user/4",
            headers={
                "If-Match": '"USER_2_0.1.0"',
                "If-Unmodified-Since": "Tue, 24 May 2021 00:00:00 GMT"
            }
        )

        assert response.status_code == 412

    def test_put_req_200_response_data(self):
        # Delete a record using the DELETE request
        response = test_client.delete(
            "/user/4",
            headers={
                "If-Match": '"USER_4_0.1.0"',
                "If-Unmodified-Since": "Tue, 24 May 2021 00:00:00 GMT"
            }
        )

        assert response.status_code == 200
