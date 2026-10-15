package com.securemsg.app.data

import android.content.Context
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Upsert
import kotlinx.coroutines.flow.Flow
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory

/** A peer's keys as first seen (trust-on-first-use) — a later change is flagged, never silently accepted. */
@Entity
data class Contact(
    @PrimaryKey val username: String,
    val signPk: String,
    val boxPk: String,
    val verified: Boolean = false,      // safety number compared in person
    val pendingSignPk: String? = null,  // server offered different keys; user must review
    val pendingBoxPk: String? = null,
)

/** id = "u:<username>" for direct chats, "g:<groupId>" for groups. */
@Entity
data class Conversation(
    @PrimaryKey val id: String,
    val title: String,
    val groupId: Long? = null,
    val owner: String? = null,
    val members: String = "",           // comma-separated, groups only
    val lastText: String = "",
    val lastTs: Long = 0,
    val unread: Int = 0,
    val removed: Boolean = false,       // we are no longer in this group
)

@Entity
data class Message(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val convId: String,
    val sender: String,
    val text: String,
    val fileJson: String? = null,       // {"blob","k","name","mime","size"} for attachments
    val ts: Long,
    val outgoing: Boolean,
    val status: Int = STATUS_SENT,
    val cid: String? = null,
) {
    companion object {
        const val STATUS_SENDING = 0
        const val STATUS_SENT = 1
        const val STATUS_FAILED = 2
    }
}

@Dao
interface ChatDao {
    @Query("SELECT * FROM Conversation WHERE removed = 0 ORDER BY lastTs DESC")
    fun conversations(): Flow<List<Conversation>>

    @Query("SELECT * FROM Conversation WHERE id = :id")
    suspend fun conversation(id: String): Conversation?

    @Query("SELECT * FROM Conversation WHERE id = :id")
    fun conversationFlow(id: String): Flow<Conversation?>

    @Upsert
    suspend fun upsertConversation(c: Conversation)

    @Query("UPDATE Conversation SET unread = 0 WHERE id = :id")
    suspend fun markRead(id: String)

    @Query("SELECT * FROM Message WHERE convId = :convId ORDER BY ts, id")
    fun messages(convId: String): Flow<List<Message>>

    @Insert
    suspend fun insertMessage(m: Message): Long

    @Query("UPDATE Message SET status = :status, ts = CASE WHEN :ts > 0 THEN :ts ELSE ts END WHERE cid = :cid")
    suspend fun setStatus(cid: String, status: Int, ts: Long)

    @Query("SELECT * FROM Message WHERE cid = :cid")
    suspend fun byCid(cid: String): Message?

    @Query("SELECT * FROM Message WHERE outgoing = 1 AND status = 0 ORDER BY id")
    suspend fun unsent(): List<Message>

    @Query("SELECT * FROM Conversation WHERE groupId IS NOT NULL")
    suspend fun groupConversations(): List<Conversation>

    @Query("SELECT * FROM Contact WHERE username = :username")
    suspend fun contact(username: String): Contact?

    @Query("SELECT * FROM Contact WHERE username = :username")
    fun contactFlow(username: String): Flow<Contact?>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putContact(c: Contact)
}

@Database(entities = [Contact::class, Conversation::class, Message::class], version = 1, exportSchema = false)
abstract class AppDb : RoomDatabase() {
    abstract fun dao(): ChatDao

    companion object {
        fun open(context: Context, passphrase: ByteArray): AppDb {
            System.loadLibrary("sqlcipher")
            return Room.databaseBuilder(context, AppDb::class.java, "chats.db")
                .openHelperFactory(SupportOpenHelperFactory(passphrase))
                .build()
        }
    }
}
