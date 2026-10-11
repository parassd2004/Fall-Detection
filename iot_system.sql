-- ============================================================
-- IoT Fall Detection System - Database Schema
-- SQL Server | Pokhara University Final Year Project
-- ============================================================

CREATE DATABASE iot_system;
GO
USE iot_system;
GO

-- ============================================================
-- USERS TABLE
-- Stores caregiver accounts with hashed passwords
-- ============================================================
CREATE TABLE users (
    id           INT IDENTITY(1,1) PRIMARY KEY,
    email        VARCHAR(100)  NOT NULL,
    password     VARCHAR(255)  NOT NULL,   -- bcrypt hashed
    full_name    VARCHAR(100)  NULL,
    is_verified  BIT           DEFAULT 0,
    created_at   DATETIME      DEFAULT GETDATE(),
    last_login   DATETIME      NULL,
    CONSTRAINT UQ_users_email UNIQUE (email)
);
GO

-- ============================================================
-- SENSOR DATA TABLE
-- Stores every reading from ESP32
-- Linked to user so fall history is per-caregiver
-- ============================================================
CREATE TABLE sensor_data (
    id       INT IDENTITY(1,1) PRIMARY KEY,
    user_id  INT           NOT NULL,
    acc      FLOAT         NOT NULL,
    gyro     FLOAT         NOT NULL,
    fall     BIT           NOT NULL DEFAULT 0,
    time     DATETIME      DEFAULT GETDATE(),
    CONSTRAINT FK_sensor_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);
GO

-- ============================================================
-- FALL EVENTS TABLE
-- Dedicated table for confirmed fall events
-- Faster to query than scanning all sensor_data
-- ============================================================
CREATE TABLE fall_events (
    id         INT IDENTITY(1,1) PRIMARY KEY,
    user_id    INT           NOT NULL,
    acc        FLOAT         NOT NULL,
    gyro       FLOAT         NOT NULL,
    time       DATETIME      DEFAULT GETDATE(),
    email_sent BIT           DEFAULT 0,
    CONSTRAINT FK_fall_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
);
GO
-- ============================================================
CREATE TABLE sessions (
    token      VARCHAR(64)  PRIMARY KEY,
    user_id    INT          NOT NULL,
    email      VARCHAR(100) NOT NULL,
    full_name  VARCHAR(100) NULL,
    expires_at DATETIME     NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ============================================================
-- INDEXES for performance
-- Most queries are ORDER BY time DESC per user
-- ============================================================
CREATE INDEX IX_sensor_user_time  ON sensor_data  (user_id, time DESC);
CREATE INDEX IX_fall_user_time    ON fall_events   (user_id, time DESC);
GO

-- ============================================================
-- VERIFY SETUP
-- ============================================================
SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_CATALOG = 'iot_system';
GO
SELECT * FROM sensor_data ;
SELECT * FROM users;
